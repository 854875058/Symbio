"""Symbio 端到端集成测试

使用 httpx.AsyncClient + ASGITransport 对真实 FastAPI 应用进行端到端测试。
每个测试使用独立的测试数据库，测试完成后自动清理。
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# 确保 src 在 Python 路径中
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.interfaces import database as db_module
from symbio.interfaces.database import Database
from symbio.interfaces.api import app

# 测试数据库路径（绝对路径，避免工作目录问题）
TEST_DB_PATH = str(PROJECT_ROOT / "data" / "test_symbio.db")


# ================================================================
# Fixtures
# ================================================================


@pytest_asyncio.fixture(autouse=True)
async def test_database():
    """每个测试使用独立的测试数据库，替代生产数据库。"""
    # 关闭已有连接
    if db_module._db_instance is not None:
        try:
            await db_module._db_instance.close()
        except Exception:
            pass
    db_module._db_instance = None

    # 删除旧的测试数据库
    db_path = Path(TEST_DB_PATH)
    if db_path.exists():
        db_path.unlink(missing_ok=True)

    # 创建新的测试数据库（包含种子数据）
    test_db = Database(TEST_DB_PATH)
    await test_db.connect()

    # 替换全局单例
    db_module._db_instance = test_db

    yield test_db

    # 清理
    try:
        await test_db.close()
    except Exception:
        pass
    db_module._db_instance = None
    if db_path.exists():
        db_path.unlink(missing_ok=True)


@pytest_asyncio.fixture
async def client(test_database):
    """创建 HTTP 测试客户端，patch get_db 使其返回测试数据库。"""

    async def mock_get_db(db_path=None):
        return test_database

    with patch("symbio.interfaces.api.get_db", mock_get_db):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


# ================================================================
# 1. 基础端点测试
# ================================================================


class TestRootEndpoints:
    """根路径与健康检查"""

    async def test_root(self, client):
        """GET / 返回应用信息"""
        resp = await client.get("/")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Symbio"
        assert data["version"] == "0.1.0"
        assert data["status"] == "running"

    async def test_health(self, client):
        """GET /health 返回 ok"""
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


# ================================================================
# 2. 会话 API 测试
# ================================================================


class TestSessionAPI:
    """会话相关接口"""

    async def test_list_sessions_has_seed_data(self, client):
        """GET /api/sessions 返回种子数据中的默认会话"""
        resp = await client.get("/api/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert "sessions" in data
        assert data["total"] >= 1
        # 种子数据中有一个 "default" 会话
        session_ids = [s["id"] for s in data["sessions"]]
        assert "default" in session_ids

    async def test_get_session_messages(self, client):
        """GET /api/sessions/default/messages 返回消息列表"""
        resp = await client.get("/api/sessions/default/messages")
        assert resp.status_code == 200
        data = resp.json()
        assert "messages" in data
        assert "total" in data
        assert data["session_id"] == "default"
        # 新种子会话没有消息
        assert isinstance(data["messages"], list)

    async def test_get_messages_nonexistent_session(self, client):
        """GET /api/sessions/nonexistent/messages 返回 404"""
        resp = await client.get("/api/sessions/nonexistent/messages")
        assert resp.status_code == 404
        assert "不存在" in resp.json()["detail"]


# ================================================================
# 3. 聊天 API 测试（Mock Anthropic）
# ================================================================


def _make_mock_settings(api_key="test-api-key"):
    """创建一个 mock Settings 对象，避免读取 symbio.yaml 文件。"""
    from symbio.config.settings import Settings
    mock_settings = MagicMock(spec=Settings)
    mock_settings.model = MagicMock()
    mock_settings.model.anthropic_api_key = api_key
    mock_settings.model.anthropic_base_url = "https://api.anthropic.com"
    mock_settings.model.model_medium = "claude-sonnet-4-20250514"
    return mock_settings


def _make_mock_anthropic_client(response_text="mocked response"):
    """创建一个 mock Anthropic 客户端。"""
    mock_content_block = MagicMock()
    mock_content_block.text = response_text

    mock_usage = MagicMock()
    mock_usage.input_tokens = 10
    mock_usage.output_tokens = 20

    mock_response = MagicMock()
    mock_response.content = [mock_content_block]
    mock_response.usage = mock_usage

    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)
    return mock_client, mock_usage


class TestChatAPI:
    """聊天接口（mock LLM 调用）"""

    async def test_chat_success(self, client):
        """POST /api/chat 成功对话，消息持久化到数据库"""
        mock_client, mock_usage = _make_mock_anthropic_client("你好！我是 Symbio 助手。")
        mock_settings = _make_mock_settings(api_key="test-key")

        with (
            patch("anthropic.AsyncAnthropic", return_value=mock_client),
            patch("symbio.interfaces.api._load_llm_settings", return_value=mock_settings),
        ):
            resp = await client.post(
                "/api/chat",
                json={"message": "你好", "session_id": "test-chat-session"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "你好" in data["content"]
        assert data["session_id"] == "test-chat-session"
        assert data["token_usage"]["input"] == 10
        assert data["token_usage"]["output"] == 20

    async def test_chat_persists_messages(self, client, test_database):
        """POST /api/chat 后消息应持久化到数据库"""
        mock_client, _ = _make_mock_anthropic_client("回复内容")
        mock_settings = _make_mock_settings(api_key="test-key")
        session_id = "persist-test"

        with (
            patch("anthropic.AsyncAnthropic", return_value=mock_client),
            patch("symbio.interfaces.api._load_llm_settings", return_value=mock_settings),
        ):
            await client.post(
                "/api/chat",
                json={"message": "测试消息", "session_id": session_id},
            )

        # 验证消息已持久化
        msgs = await test_database.list_messages_by_session(session_id)
        assert len(msgs) == 2  # user + assistant
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "测试消息"
        assert msgs[1]["role"] == "assistant"
        assert msgs[1]["content"] == "回复内容"


# ================================================================
# 4. 任务 API 测试
# ================================================================


class TestTaskAPI:
    """任务相关接口"""

    async def test_list_tasks(self, client):
        """GET /api/tasks 返回种子任务"""
        resp = await client.get("/api/tasks")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert len(data["tasks"]) == 5
        # 每个任务应包含 steps 字段
        for task in data["tasks"]:
            assert "steps" in task
            assert isinstance(task["steps"], list)

    async def test_list_tasks_filter_completed(self, client):
        """GET /api/tasks?status=completed 只返回已完成任务"""
        resp = await client.get("/api/tasks", params={"status": "completed"})
        assert resp.status_code == 200
        data = resp.json()
        # 种子数据中有 2 个 completed 任务 (t-001, t-004)
        assert data["total"] == 2
        for task in data["tasks"]:
            assert task["status"] == "completed"

    async def test_list_tasks_filter_running(self, client):
        """GET /api/tasks?status=running 只返回运行中任务"""
        resp = await client.get("/api/tasks", params={"status": "running"})
        assert resp.status_code == 200
        data = resp.json()
        # 种子数据中有 2 个 running 任务 (t-002, t-005)
        assert data["total"] == 2
        for task in data["tasks"]:
            assert task["status"] == "running"

    async def test_get_task_detail(self, client):
        """GET /api/tasks/t-001 返回任务详情（含步骤）"""
        resp = await client.get("/api/tasks/t-001")
        assert resp.status_code == 200
        task = resp.json()["task"]
        assert task["id"] == "t-001"
        assert task["name"] == "代码审查: api.py"
        assert task["status"] == "completed"
        assert task["agent"] == "general_agent"
        # t-001 有 3 个步骤
        assert len(task["steps"]) == 3
        for step in task["steps"]:
            assert step["status"] == "completed"

    async def test_get_task_nonexistent(self, client):
        """GET /api/tasks/nonexistent 返回 404"""
        resp = await client.get("/api/tasks/nonexistent")
        assert resp.status_code == 404
        assert "不存在" in resp.json()["detail"]


# ================================================================
# 5. 模型 API 测试
# ================================================================


class TestModelAPI:
    """模型管理接口"""

    async def test_list_models(self, client):
        """GET /api/models 返回种子模型"""
        resp = await client.get("/api/models")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["models"]) == 3
        model_ids = [m["model_id"] for m in data["models"]]
        assert "claude-3-5-haiku-20241022" in model_ids
        assert "claude-sonnet-4-20250514" in model_ids
        assert "claude-opus-4-20250514" in model_ids

    async def test_create_model(self, client):
        """POST /api/models 创建新模型"""
        resp = await client.post(
            "/api/models",
            json={
                "model_id": "gpt-4o-test",
                "provider": "openai",
                "display_name": "GPT-4o Test",
            },
        )
        assert resp.status_code == 200
        model = resp.json()["model"]
        assert model["model_id"] == "gpt-4o-test"
        assert model["provider"] == "openai"
        assert model["display_name"] == "GPT-4o Test"

        # 验证确实添加到了数据库
        list_resp = await client.get("/api/models")
        model_ids = [m["model_id"] for m in list_resp.json()["models"]]
        assert "gpt-4o-test" in model_ids

    async def test_delete_model(self, client, test_database):
        """DELETE /api/models/{id} 删除模型"""
        # 先创建一个模型
        model = await test_database.create_model(
            model_id="to-delete", provider="test", display_name="Delete Me"
        )
        model_id = model["id"]

        resp = await client.delete(f"/api/models/{model_id}")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        # 验证已被删除
        list_resp = await client.get("/api/models")
        ids = [m["id"] for m in list_resp.json()["models"]]
        assert model_id not in ids

    async def test_delete_model_nonexistent(self, client):
        """DELETE /api/models/nonexistent 返回 404"""
        resp = await client.delete("/api/models/nonexistent")
        assert resp.status_code == 404
        assert "不存在" in resp.json()["detail"]


# ================================================================
# 6. 记忆 API 测试
# ================================================================


class TestMemoryAPI:
    """记忆管理接口"""

    async def test_list_memories(self, client):
        """GET /api/memory 返回种子记忆"""
        resp = await client.get("/api/memory")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert len(data["memories"]) == 5

    async def test_search_memories(self, client):
        """GET /api/memory/search?q=DAG 搜索相关记忆"""
        resp = await client.get("/api/memory/search", params={"q": "DAG"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["query"] == "DAG"
        assert len(data["memories"]) >= 1
        # 应包含 "DAG 引擎设计模式" 记忆
        titles = [m["title"] for m in data["memories"]]
        assert any("DAG" in t for t in titles)
        # 搜索结果应包含 relevance 字段
        for mem in data["memories"]:
            assert "relevance" in mem

    async def test_search_memories_empty_query(self, client):
        """GET /api/memory/search?q= 空查询返回全部记忆"""
        resp = await client.get("/api/memory/search", params={"q": ""})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["memories"]) == 5

    async def test_search_memories_relevance_ordering(self, client):
        """搜索结果应按 relevance 降序排列"""
        resp = await client.get("/api/memory/search", params={"q": "DAG"})
        results = resp.json()["memories"]
        if len(results) > 1:
            relevances = [m["relevance"] for m in results]
            assert relevances == sorted(relevances, reverse=True)


# ================================================================
# 7. 技能 API 测试
# ================================================================


class TestSkillAPI:
    """技能管理接口"""

    async def test_list_skills(self, client):
        """GET /api/skills 返回种子技能"""
        resp = await client.get("/api/skills")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 6
        assert len(data["skills"]) == 6

    async def test_import_skill(self, client):
        """POST /api/skills/import 导入新技能"""
        resp = await client.post(
            "/api/skills/import",
            json={
                "name": "my-custom-skill",
                "description": "A custom test skill",
                "version": "0.1.0",
                "trigger_keywords": ["custom", "test"],
            },
        )
        assert resp.status_code == 200
        skill = resp.json()["skill"]
        assert skill["name"] == "my-custom-skill"
        assert skill["description"] == "A custom test skill"
        assert skill["trigger_keywords"] == ["custom", "test"]

    async def test_update_skill(self, client, test_database):
        """PUT /api/skills/{id} 更新技能信息"""
        # 使用种子数据中的技能
        resp = await client.put(
            "/api/skills/sk-001",
            json={"description": "Updated description", "version": "2.0.0"},
        )
        assert resp.status_code == 200
        skill = resp.json()["skill"]
        assert skill["description"] == "Updated description"
        assert skill["version"] == "2.0.0"
        # 名称未变
        assert skill["name"] == "code-review"

    async def test_delete_skill(self, client, test_database):
        """DELETE /api/skills/{id} 删除技能"""
        # 先创建一个待删除的技能
        await test_database.create_skill(
            skill_id="sk-to-delete",
            name="deletable-skill",
            description="Will be deleted",
        )

        resp = await client.delete("/api/skills/sk-to-delete")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        # 验证已删除
        list_resp = await client.get("/api/skills")
        skill_ids = [s["id"] for s in list_resp.json()["skills"]]
        assert "sk-to-delete" not in skill_ids

    async def test_update_skill_nonexistent(self, client):
        """PUT /api/skills/nonexistent 返回 404"""
        resp = await client.put(
            "/api/skills/nonexistent",
            json={"name": "no-such-skill"},
        )
        assert resp.status_code == 404

    async def test_delete_skill_nonexistent(self, client):
        """DELETE /api/skills/nonexistent 返回 404"""
        resp = await client.delete("/api/skills/nonexistent")
        assert resp.status_code == 404


# ================================================================
# 8. 配置 API 测试
# ================================================================


class TestConfigAPI:
    """配置管理接口"""

    async def test_get_config(self, client):
        """GET /api/config 返回配置字典"""
        mock_settings = _make_mock_settings(api_key="")
        mock_settings.model.openai_api_key = ""
        mock_settings.model.openai_base_url = "https://api.openai.com/v1"
        mock_settings.model.model_low = "claude-3-5-haiku-20241022"
        mock_settings.model.model_high = "claude-opus-4-20250514"

        with patch("symbio.interfaces.api._load_llm_settings", return_value=mock_settings):
            resp = await client.get("/api/config")

        assert resp.status_code == 200
        data = resp.json()
        expected_keys = [
            "anthropic_api_key",
            "anthropic_base_url",
            "openai_api_key",
            "openai_base_url",
            "model_low",
            "model_medium",
            "model_high",
        ]
        for key in expected_keys:
            assert key in data, f"Missing config key: {key}"

    async def test_update_config(self, client, tmp_path):
        """POST /api/config 更新配置"""
        # 使用临时目录避免污染项目目录
        config_file = tmp_path / "symbio.yaml"
        mock_settings = _make_mock_settings(api_key="")
        mock_settings.model.openai_api_key = ""
        mock_settings.model.openai_base_url = "https://api.openai.com/v1"
        mock_settings.model.model_low = "claude-3-5-haiku-20241022"
        mock_settings.model.model_high = "claude-opus-4-20250514"
        mock_settings.model_dump = MagicMock(return_value={
            "model": {
                "anthropic_api_key": "",
                "anthropic_base_url": "https://api.anthropic.com",
                "openai_api_key": "",
                "openai_base_url": "https://api.openai.com/v1",
                "model_low": "claude-3-5-haiku-20241022",
                "model_medium": "claude-sonnet-4-20250514",
                "model_high": "claude-opus-4-20250514",
            }
        })

        with (
            patch("symbio.interfaces.api._load_llm_settings", return_value=mock_settings),
            patch("symbio.config.settings.Settings.from_yaml", return_value=mock_settings),
        ):
            resp = await client.post(
                "/api/config",
                json={
                    "anthropic_api_key": "test-key-123",
                    "model_medium": "test-model-v2",
                },
            )

        assert resp.status_code == 200
        assert resp.json()["success"] is True


# ================================================================
# 9. 数据完整性测试
# ================================================================


class TestDataIntegrity:
    """验证种子数据的完整性和结构"""

    async def test_seed_sessions_count(self, client):
        """种子数据包含 1 个默认会话"""
        resp = await client.get("/api/sessions")
        assert resp.json()["total"] == 1

    async def test_seed_models_count(self, client):
        """种子数据包含 3 个模型"""
        resp = await client.get("/api/models")
        models = resp.json()["models"]
        assert len(models) == 3

    async def test_seed_skills_count(self, client):
        """种子数据包含 6 个技能"""
        resp = await client.get("/api/skills")
        assert resp.json()["total"] == 6

    async def test_seed_memories_count(self, client):
        """种子数据包含 5 条记忆"""
        resp = await client.get("/api/memory")
        assert resp.json()["total"] == 5

    async def test_seed_tasks_count(self, client):
        """种子数据包含 5 个任务"""
        resp = await client.get("/api/tasks")
        assert resp.json()["total"] == 5

    async def test_tasks_have_steps(self, client):
        """每个种子任务都应有步骤"""
        resp = await client.get("/api/tasks")
        for task in resp.json()["tasks"]:
            assert len(task["steps"]) > 0, f"Task {task['id']} has no steps"

    async def test_skills_have_trigger_keywords(self, client):
        """种子技能应有触发关键词"""
        resp = await client.get("/api/skills")
        for skill in resp.json()["skills"]:
            assert isinstance(skill["trigger_keywords"], list)
            assert len(skill["trigger_keywords"]) > 0, (
                f"Skill {skill['name']} has no trigger_keywords"
            )

    async def test_memories_have_tags(self, client):
        """种子记忆应有标签"""
        resp = await client.get("/api/memory")
        for mem in resp.json()["memories"]:
            assert isinstance(mem["tags"], list)
            assert len(mem["tags"]) > 0, f"Memory {mem['id']} has no tags"

    async def test_task_step_statuses(self, client):
        """验证 t-001 任务的步骤状态"""
        resp = await client.get("/api/tasks/t-001")
        steps = resp.json()["task"]["steps"]
        assert len(steps) == 3
        step_names = [s["name"] for s in steps]
        assert "加载代码" in step_names
        assert "静态分析" in step_names
        assert "生成报告" in step_names

    async def test_memory_importance_range(self, client):
        """记忆的重要性应在 0-1 范围内"""
        resp = await client.get("/api/memory")
        for mem in resp.json()["memories"]:
            assert 0.0 <= mem["importance"] <= 1.0


# ================================================================
# 10. 数据库持久化测试
# ================================================================


class TestDatabasePersistence:
    """验证数据持久化"""

    async def test_create_session_persists(self, client, test_database):
        """创建的会话能持久化"""
        session_id = "persist-session-test"
        await test_database.create_session(session_id, title="持久化测试")

        resp = await client.get("/api/sessions")
        session_ids = [s["id"] for s in resp.json()["sessions"]]
        assert session_id in session_ids

    async def test_create_message_persists(self, client, test_database):
        """添加的消息能持久化"""
        session_id = "msg-persist-test"
        await test_database.create_session(session_id, title="消息持久化")
        await test_database.create_message(
            "msg-test-001", session_id, "user", "持久化测试消息", "2026-06-01T10:00:00", 0
        )

        resp = await client.get(f"/api/sessions/{session_id}/messages")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["messages"][0]["content"] == "持久化测试消息"

    async def test_model_persists_after_connection(self, client, test_database):
        """模型数据在连接期间保持不变"""
        # 创建模型
        await test_database.create_model(
            model_id="persist-model", provider="test", display_name="Persist Test"
        )

        # 通过 API 查询
        resp = await client.get("/api/models")
        model_ids = [m["model_id"] for m in resp.json()["models"]]
        assert "persist-model" in model_ids

    async def test_search_memories_with_keywords(self, client, test_database):
        """搜索记忆应返回按相关度排序的结果"""
        # 搜索 "Python" - 应匹配 "Python 快速排序算法"
        resp = await client.get("/api/memory/search", params={"q": "Python"})
        data = resp.json()
        assert len(data["memories"]) >= 1
        # 第一个结果的标题应包含 "Python"
        assert "Python" in data["memories"][0]["title"]

    async def test_search_memories_with_tag_keyword(self, client):
        """通过标签关键词搜索记忆"""
        # 搜索 "算法" - 这是 mem-001 的标签
        resp = await client.get("/api/memory/search", params={"q": "算法"})
        data = resp.json()
        assert len(data["memories"]) >= 1


# ================================================================
# 11. 错误处理测试
# ================================================================


class TestErrorHandling:
    """异常场景与错误处理"""

    async def test_invalid_session_id_returns_404(self, client):
        """访问不存在的会话返回 404"""
        resp = await client.get("/api/sessions/does-not-exist/messages")
        assert resp.status_code == 404

    async def test_invalid_task_id_returns_404(self, client):
        """访问不存在的任务返回 404"""
        resp = await client.get("/api/tasks/does-not-exist")
        assert resp.status_code == 404

    async def test_duplicate_skill_name_returns_400(self, client):
        """导入已存在的技能名称返回 400"""
        # 种子数据中已有 "code-review" 技能
        resp = await client.post(
            "/api/skills/import",
            json={
                "name": "code-review",
                "description": "Duplicate skill attempt",
            },
        )
        assert resp.status_code == 400
        assert "已存在" in resp.json()["detail"]

    async def test_delete_nonexistent_model_returns_404(self, client):
        """删除不存在的模型返回 404"""
        resp = await client.delete("/api/models/nonexistent-model-id")
        assert resp.status_code == 404

    async def test_delete_nonexistent_skill_returns_404(self, client):
        """删除不存在的技能返回 404"""
        resp = await client.delete("/api/skills/nonexistent-skill-id")
        assert resp.status_code == 404

    async def test_update_nonexistent_skill_returns_404(self, client):
        """更新不存在的技能返回 404"""
        resp = await client.put(
            "/api/skills/nonexistent-skill-id",
            json={"name": "nope"},
        )
        assert resp.status_code == 404


# ================================================================
# 12. 额外边界测试
# ================================================================


class TestEdgeCases:
    """边界条件与额外场景"""

    async def test_chat_default_session(self, client):
        """POST /api/chat 不指定 session_id 时使用 default"""
        mock_client, _ = _make_mock_anthropic_client("OK")
        mock_settings = _make_mock_settings(api_key="test-key")

        with (
            patch("anthropic.AsyncAnthropic", return_value=mock_client),
            patch("symbio.interfaces.api._load_llm_settings", return_value=mock_settings),
        ):
            resp = await client.post(
                "/api/chat",
                json={"message": "test"},
                # 不传 session_id
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "default"

    async def test_create_model_with_defaults(self, client):
        """POST /api/models 使用默认值"""
        resp = await client.post(
            "/api/models",
            json={"model_id": "test-defaults"},
        )
        assert resp.status_code == 200
        model = resp.json()["model"]
        assert model["provider"] == "anthropic"
        assert model["enabled"] is True
        assert model["base_url"] == "https://api.anthropic.com"

    async def test_skill_search(self, client):
        """GET /api/skills/search?q=doc 搜索技能"""
        resp = await client.get("/api/skills/search", params={"q": "doc"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["skills"]) >= 1
        # 应匹配 "doc-writer" 技能
        names = [s["name"] for s in data["skills"]]
        assert any("doc" in n for n in names)

    async def test_list_tasks_all_status(self, client):
        """GET /api/tasks?status=all 返回所有任务"""
        resp = await client.get("/api/tasks", params={"status": "all"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 5

    async def test_memory_access_count(self, client):
        """种子记忆的访问次数应为非负整数"""
        resp = await client.get("/api/memory")
        for mem in resp.json()["memories"]:
            assert isinstance(mem["access_count"], int)
            assert mem["access_count"] >= 0

    async def test_skill_enabled_field(self, client):
        """种子技能中 security-scanner 应被禁用"""
        resp = await client.get("/api/skills")
        skills = {s["name"]: s for s in resp.json()["skills"]}
        assert skills["security-scanner"]["enabled"] is False
        assert skills["code-review"]["enabled"] is True
