"""Symbio SQLite 持久化层

使用 aiosqlite 提供异步数据库操作，替代 api.py 中的内存数据存储。
支持会话、消息、模型、任务、记忆、技能、配置等全表 CRUD。
"""

from __future__ import annotations

import json
import uuid
import time
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from symbio.utils.logger import get_logger

logger = get_logger("database")

# 默认数据库路径
DEFAULT_DB_PATH = "./data/symbio.db"

# 单例实例
_db_instance: Optional["Database"] = None


class Database:
    """异步 SQLite 数据库封装

    提供表创建、种子数据插入、以及各表的完整 CRUD 操作。
    所有方法均为异步，通过 aiosqlite 实现。
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        """初始化数据库实例

        Args:
            db_path: SQLite 数据库文件路径，默认 ./data/symbio.db
        """
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        """建立数据库连接并初始化表结构和种子数据"""
        # 确保目录存在
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self._db = await aiosqlite.connect(self.db_path)
        # 启用外键约束
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")

        logger.info(f"数据库已连接: {self.db_path}")

        # 创建表结构
        await self._create_tables()

        # 插入种子数据（仅在表为空时）
        await self._seed_data()

    async def close(self) -> None:
        """关闭数据库连接"""
        if self._db:
            await self._db.close()
            self._db = None
            logger.info("数据库连接已关闭")

    @property
    def db(self) -> aiosqlite.Connection:
        """获取数据库连接，未连接时抛出异常"""
        if self._db is None:
            raise RuntimeError("数据库未连接，请先调用 connect()")
        return self._db

    # ================================================================
    # 表结构创建
    # ================================================================

    async def _create_tables(self) -> None:
        """创建所有数据表，使用 IF NOT EXISTS 确保幂等"""
        await self.db.executescript("""
            -- 会话表
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '新对话',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                message_count INTEGER DEFAULT 0
            );

            -- 消息表
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                tokens INTEGER DEFAULT 0,
                sequence INTEGER,
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );

            -- 模型表
            CREATE TABLE IF NOT EXISTS models (
                id TEXT PRIMARY KEY,
                model_id TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'anthropic',
                display_name TEXT NOT NULL DEFAULT '',
                api_key TEXT DEFAULT '',
                base_url TEXT DEFAULT 'https://api.anthropic.com',
                enabled BOOLEAN DEFAULT 1,
                created_at TEXT NOT NULL
            );

            -- 任务表
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                agent TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                completed_at TEXT,
                description TEXT DEFAULT '',
                result TEXT
            );

            -- 任务步骤表
            CREATE TABLE IF NOT EXISTS task_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                duration TEXT,
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );

            -- 记忆表
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                tags TEXT DEFAULT '[]',
                source TEXT DEFAULT 'chat',
                importance REAL DEFAULT 0.5,
                created_at TEXT NOT NULL,
                access_count INTEGER DEFAULT 0
            );

            -- 技能表
            CREATE TABLE IF NOT EXISTS skills (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                version TEXT DEFAULT '1.0.0',
                source TEXT DEFAULT 'builtin',
                enabled BOOLEAN DEFAULT 1,
                trigger_keywords TEXT DEFAULT '[]',
                created_at TEXT NOT NULL
            );

            -- 配置表（键值对存储）
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            -- 创建索引以加速查询
            CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
            CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
            CREATE INDEX IF NOT EXISTS idx_task_steps_task ON task_steps(task_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        """)
        await self.db.commit()

        # Migration: add sequence column to existing messages table
        try:
            await self.db.execute(
                "ALTER TABLE messages ADD COLUMN sequence INTEGER"
            )
            await self.db.commit()
        except Exception:
            pass  # Column already exists

        logger.info("数据表结构已初始化")

    # ================================================================
    # 种子数据
    # ================================================================

    async def _seed_data(self) -> None:
        """在表为空时插入默认种子数据"""
        # 检查 sessions 表是否已有数据
        cursor = await self.db.execute("SELECT COUNT(*) FROM sessions")
        count = (await cursor.fetchone())[0]
        if count > 0:
            logger.info("种子数据已存在，跳过初始化")
            return

        logger.info("开始插入种子数据...")

        # 默认会话
        await self._seed_session()

        # 示例技能
        await self._seed_skills()

        # 示例模型
        await self._seed_models()

        # 示例记忆
        await self._seed_memories()

        # 示例任务（含步骤）
        await self._seed_tasks()

        await self.db.commit()
        logger.info("种子数据插入完成")

    async def _seed_session(self) -> None:
        """插入默认会话"""
        await self.db.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at, message_count) VALUES (?, ?, ?, ?, ?)",
            ("default", "新对话", "2026-05-28T10:00:00", "2026-05-28T10:00:00", 0),
        )

    async def _seed_skills(self) -> None:
        """插入示例技能数据"""
        skills = [
            ("sk-001", "code-review", "Review code for correctness, security, and performance issues with detailed findings",
             "1.2.0", "builtin", True, json.dumps(["代码审查", "code review", "review"]), "2026-05-20T10:00:00"),
            ("sk-002", "doc-writer", "Generate technical documentation from code, APIs, or specifications",
             "1.0.3", "builtin", True, json.dumps(["文档", "documentation", "docs"]), "2026-05-20T10:00:00"),
            ("sk-003", "data-analyst", "Analyze datasets, generate statistics, and produce visualizations",
             "0.9.1", "custom", True, json.dumps(["数据分析", "data analysis", "统计"]), "2026-05-21T14:00:00"),
            ("sk-004", "test-generator", "Automatically generate unit tests and integration tests for given code",
             "1.1.0", "builtin", True, json.dumps(["测试", "test", "单元测试"]), "2026-05-22T09:00:00"),
            ("sk-005", "security-scanner", "Scan code and dependencies for known security vulnerabilities and CVEs",
             "2.0.1", "external", False, json.dumps(["安全", "security", "CVE", "漏洞"]), "2026-05-23T16:00:00"),
            ("sk-006", "translator", "Translate text between multiple languages with context-aware accuracy",
             "1.3.2", "builtin", True, json.dumps(["翻译", "translate", "i18n"]), "2026-05-24T11:00:00"),
        ]
        await self.db.executemany(
            "INSERT INTO skills (id, name, description, version, source, enabled, trigger_keywords, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            skills,
        )

    async def _seed_models(self) -> None:
        """插入示例模型数据"""
        models = [
            ("m-001", "claude-3-5-haiku-20241022", "anthropic", "Claude 3.5 Haiku",
             "", "https://api.anthropic.com", True, "2026-05-20T10:00:00"),
            ("m-002", "claude-sonnet-4-20250514", "anthropic", "Claude Sonnet 4",
             "", "https://api.anthropic.com", True, "2026-05-20T10:00:00"),
            ("m-003", "claude-opus-4-20250514", "anthropic", "Claude Opus 4",
             "", "https://api.anthropic.com", True, "2026-05-20T10:00:00"),
        ]
        await self.db.executemany(
            "INSERT INTO models (id, model_id, provider, display_name, api_key, base_url, enabled, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            models,
        )

    async def _seed_memories(self) -> None:
        """插入示例记忆数据"""
        memories = [
            ("mem-001", "Python 快速排序算法",
             "快速排序使用分治策略，选择基准元素，将数组分成两部分，递归排序。时间复杂度 O(n log n)，最坏 O(n^2)。实现要点：选择中间元素作为 pivot，使用双指针分区。",
             json.dumps(["python", "算法", "排序"]), "chat", 0.85, "2026-05-27T14:30:00", 5),
            ("mem-002", "DAG 引擎设计模式",
             "动态 DAG 引擎用于编排多 Agent 工作流。核心概念：节点表示 Agent 任务，边表示依赖关系。支持条件分支、并行执行、错误重试。拓扑排序决定执行顺序。",
             json.dumps(["架构", "DAG", "Agent"]), "chat", 0.92, "2026-05-27T15:00:00", 8),
            ("mem-003", "数据库 Schema 设计规范",
             "设计数据库 schema 的关键原则：第三范式避免冗余，适度反范式提升查询性能。命名使用 snake_case，主键用 UUID 或自增 ID，时间字段用 TIMESTAMP WITH TIME ZONE。",
             json.dumps(["数据库", "设计", "PostgreSQL"]), "chat", 0.78, "2026-05-26T11:00:00", 3),
            ("mem-004", "Anthropic API 认证方式",
             "Anthropic API 使用 x-api-key 头部认证。Base URL 默认 https://api.anthropic.com，支持自定义。推荐使用 AsyncAnthropic 客户端进行异步调用，配合 retry 和 timeout 配置。",
             json.dumps(["API", "认证", "Anthropic"]), "system", 0.95, "2026-05-25T09:00:00", 12),
            ("mem-005", "WebSocket 心跳机制",
             "WebSocket 长连接需要心跳保活。客户端每 30 秒发送 ping 帧，服务端返回 pong。超过 3 次未收到 pong 则判定断线，触发重连逻辑。指数退避策略：1s, 2s, 4s, 8s, 最大 30s。",
             json.dumps(["WebSocket", "网络", "保活"]), "chat", 0.72, "2026-05-26T16:00:00", 2),
        ]
        await self.db.executemany(
            "INSERT INTO memories (id, title, content, tags, source, importance, created_at, access_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            memories,
        )

    async def _seed_tasks(self) -> None:
        """插入示例任务及步骤数据"""
        tasks = [
            ("t-001", "代码审查: api.py", "completed", "general_agent",
             "2026-05-28T09:30:00", "2026-05-28T09:45:00",
             "对 api.py 进行代码审查，检查安全性与性能问题",
             "发现 3 个潜在问题，已生成修复建议"),
            ("t-002", "数据清洗: 用户数据集", "running", "data_agent",
             "2026-05-28T10:00:00", None,
             "清洗用户行为数据，移除异常值和重复记录", None),
            ("t-003", "API 文档生成", "failed", "doc_agent",
             "2026-05-28T08:00:00", "2026-05-28T08:10:00",
             "自动生成 OpenAPI 文档并导出为 Markdown",
             "错误: 模板引擎渲染失败，缺少依赖模块 jinja2"),
            ("t-004", "单元测试: core/orchestrator.py", "completed", "test_agent",
             "2026-05-27T16:00:00", "2026-05-27T16:20:00",
             "运行 orchestrator 模块的单元测试套件",
             "42 个测试通过，0 个失败，覆盖率 87%"),
            ("t-005", "依赖安全扫描", "running", "security_agent",
             "2026-05-28T11:00:00", None,
             "扫描项目依赖，检查已知 CVE 漏洞", None),
        ]
        await self.db.executemany(
            "INSERT INTO tasks (id, name, status, agent, created_at, completed_at, description, result) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            tasks,
        )

        # 任务步骤
        steps = [
            ("t-001", "加载代码", "completed", "2s"),
            ("t-001", "静态分析", "completed", "8s"),
            ("t-001", "生成报告", "completed", "5s"),
            ("t-002", "读取数据源", "completed", "3s"),
            ("t-002", "去重处理", "completed", "12s"),
            ("t-002", "异常检测", "running", None),
            ("t-002", "格式标准化", "pending", None),
            ("t-003", "解析路由", "completed", "4s"),
            ("t-003", "渲染文档", "failed", "6s"),
            ("t-004", "收集测试", "completed", "1s"),
            ("t-004", "执行测试", "completed", "18s"),
            ("t-004", "覆盖率报告", "completed", "2s"),
            ("t-005", "解析 requirements", "completed", "2s"),
            ("t-005", "查询漏洞库", "running", None),
            ("t-005", "生成报告", "pending", None),
        ]
        await self.db.executemany(
            "INSERT INTO task_steps (task_id, name, status, duration) VALUES (?, ?, ?, ?)",
            steps,
        )

    # ================================================================
    # Sessions（会话）CRUD
    # ================================================================

    async def create_session(
        self,
        session_id: str,
        title: str = "新对话",
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
        message_count: int = 0,
    ) -> dict:
        """创建新会话

        Args:
            session_id: 会话 ID
            title: 会话标题
            created_at: 创建时间，为空则自动生成
            updated_at: 更新时间，为空则自动生成
            message_count: 消息计数

        Returns:
            创建的会话字典
        """
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        created_at = created_at or now
        updated_at = updated_at or now

        await self.db.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at, message_count) VALUES (?, ?, ?, ?, ?)",
            (session_id, title, created_at, updated_at, message_count),
        )
        await self.db.commit()
        logger.info(f"会话已创建: {session_id}")

        return {
            "id": session_id,
            "title": title,
            "created_at": created_at,
            "updated_at": updated_at,
            "message_count": message_count,
        }

    async def list_sessions(self) -> list[dict]:
        """获取会话列表，按更新时间倒序排列

        Returns:
            会话字典列表
        """
        cursor = await self.db.execute(
            "SELECT id, title, created_at, updated_at, message_count FROM sessions ORDER BY updated_at DESC"
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "title": row[1],
                "created_at": row[2],
                "updated_at": row[3],
                "message_count": row[4],
            }
            for row in rows
        ]

    async def get_session(self, session_id: str) -> Optional[dict]:
        """根据 ID 获取会话

        Args:
            session_id: 会话 ID

        Returns:
            会话字典，不存在则返回 None
        """
        cursor = await self.db.execute(
            "SELECT id, title, created_at, updated_at, message_count FROM sessions WHERE id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "title": row[1],
            "created_at": row[2],
            "updated_at": row[3],
            "message_count": row[4],
        }

    async def update_session(self, session_id: str, **kwargs) -> Optional[dict]:
        """更新会话信息

        Args:
            session_id: 会话 ID
            **kwargs: 可更新字段（title, updated_at, message_count）

        Returns:
            更新后的会话字典，不存在则返回 None
        """
        # 检查会话是否存在
        existing = await self.get_session(session_id)
        if existing is None:
            return None

        # 构建动态 UPDATE 语句
        allowed_fields = {"title", "updated_at", "message_count"}
        updates = {k: v for k, v in kwargs.items() if k in allowed_fields and v is not None}

        if not updates:
            return existing

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [session_id]

        await self.db.execute(f"UPDATE sessions SET {set_clause} WHERE id = ?", values)
        await self.db.commit()

        return await self.get_session(session_id)

    async def delete_session(self, session_id: str) -> bool:
        """删除会话及其关联消息

        Args:
            session_id: 会话 ID

        Returns:
            是否删除成功（False 表示会话不存在）
        """
        existing = await self.get_session(session_id)
        if existing is None:
            return False

        # 先删除关联消息
        await self.db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        # 再删除会话
        await self.db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        await self.db.commit()
        logger.info(f"会话已删除: {session_id}")
        return True

    # ================================================================
    # Messages（消息）CRUD
    # ================================================================

    async def create_message(
        self,
        message_id: str,
        session_id: str,
        role: str,
        content: str,
        timestamp: Optional[str] = None,
        tokens: int = 0,
    ) -> dict:
        """创建新消息

        Args:
            message_id: 消息 ID
            session_id: 所属会话 ID
            role: 角色（user/assistant/system）
            content: 消息内容
            timestamp: 时间戳，为空则自动生成
            tokens: token 消耗量

        Returns:
            创建的消息字典
        """
        timestamp = timestamp or time.strftime("%Y-%m-%dT%H:%M:%S")

        # Auto-increment sequence within session for stable ordering
        cursor = await self.db.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM messages WHERE session_id = ?",
            (session_id,),
        )
        next_seq = (await cursor.fetchone())[0]

        await self.db.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp, tokens, sequence) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (message_id, session_id, role, content, timestamp, tokens, next_seq),
        )
        await self.db.commit()

        # 更新会话的消息计数和更新时间
        await self._refresh_session_stats(session_id)

        return {
            "id": message_id,
            "session_id": session_id,
            "role": role,
            "content": content,
            "timestamp": timestamp,
            "tokens": tokens,
        }

    async def list_messages_by_session(self, session_id: str) -> list[dict]:
        """获取指定会话的消息列表，按 sequence 正序排列（sequence 为空时回退到 timestamp）

        Args:
            session_id: 会话 ID

        Returns:
            消息字典列表
        """
        cursor = await self.db.execute(
            "SELECT id, session_id, role, content, timestamp, tokens FROM messages "
            "WHERE session_id = ? ORDER BY COALESCE(sequence, 0), timestamp ASC",
            (session_id,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "session_id": row[1],
                "role": row[2],
                "content": row[3],
                "timestamp": row[4],
                "tokens": row[5],
            }
            for row in rows
        ]

    async def get_message(self, message_id: str) -> Optional[dict]:
        """根据 ID 获取消息

        Args:
            message_id: 消息 ID

        Returns:
            消息字典，不存在则返回 None
        """
        cursor = await self.db.execute(
            "SELECT id, session_id, role, content, timestamp, tokens FROM messages WHERE id = ?",
            (message_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "session_id": row[1],
            "role": row[2],
            "content": row[3],
            "timestamp": row[4],
            "tokens": row[5],
        }

    async def _refresh_session_stats(self, session_id: str) -> None:
        """刷新会话的消息计数和更新时间

        当消息增删时自动调用。
        """
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        await self.db.execute(
            "UPDATE sessions SET message_count = (SELECT COUNT(*) FROM messages WHERE session_id = ?), "
            "updated_at = ? WHERE id = ?",
            (session_id, now, session_id),
        )
        await self.db.commit()

    # ================================================================
    # Models（模型）CRUD
    # ================================================================

    async def create_model(
        self,
        model_id: str,
        provider: str = "anthropic",
        display_name: str = "",
        api_key: str = "",
        base_url: str = "https://api.anthropic.com",
        enabled: bool = True,
        created_at: Optional[str] = None,
    ) -> dict:
        """添加新模型

        Args:
            model_id: 模型标识符（如 claude-sonnet-4-20250514）
            provider: 提供商名称
            display_name: 显示名称
            api_key: API 密钥
            base_url: API 基础 URL
            enabled: 是否启用
            created_at: 创建时间

        Returns:
            创建的模型字典
        """
        record_id = f"m-{uuid.uuid4().hex[:8]}"
        created_at = created_at or time.strftime("%Y-%m-%dT%H:%M:%S")

        await self.db.execute(
            "INSERT INTO models (id, model_id, provider, display_name, api_key, base_url, enabled, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (record_id, model_id, provider, display_name or model_id, api_key, base_url, enabled, created_at),
        )
        await self.db.commit()
        logger.info(f"模型已添加: {display_name or model_id}")

        return {
            "id": record_id,
            "model_id": model_id,
            "provider": provider,
            "display_name": display_name or model_id,
            "api_key": api_key,
            "base_url": base_url,
            "enabled": enabled,
            "created_at": created_at,
        }

    async def list_models(self) -> list[dict]:
        """获取所有模型列表

        Returns:
            模型字典列表
        """
        cursor = await self.db.execute(
            "SELECT id, model_id, provider, display_name, api_key, base_url, enabled, created_at FROM models "
            "ORDER BY created_at ASC"
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "model_id": row[1],
                "provider": row[2],
                "display_name": row[3],
                "api_key": row[4],
                "base_url": row[5],
                "enabled": bool(row[6]),
                "created_at": row[7],
            }
            for row in rows
        ]

    async def get_model(self, model_id: str) -> Optional[dict]:
        """根据 ID 获取模型

        Args:
            model_id: 模型记录 ID

        Returns:
            模型字典，不存在则返回 None
        """
        cursor = await self.db.execute(
            "SELECT id, model_id, provider, display_name, api_key, base_url, enabled, created_at "
            "FROM models WHERE id = ?",
            (model_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "model_id": row[1],
            "provider": row[2],
            "display_name": row[3],
            "api_key": row[4],
            "base_url": row[5],
            "enabled": bool(row[6]),
            "created_at": row[7],
        }

    async def update_model(self, model_id: str, **kwargs) -> Optional[dict]:
        """更新模型信息

        Args:
            model_id: 模型记录 ID
            **kwargs: 可更新字段

        Returns:
            更新后的模型字典，不存在则返回 None
        """
        existing = await self.get_model(model_id)
        if existing is None:
            return None

        allowed_fields = {"model_id", "provider", "display_name", "api_key", "base_url", "enabled"}
        updates = {k: v for k, v in kwargs.items() if k in allowed_fields}

        if not updates:
            return existing

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [model_id]

        await self.db.execute(f"UPDATE models SET {set_clause} WHERE id = ?", values)
        await self.db.commit()

        return await self.get_model(model_id)

    async def delete_model(self, model_id: str) -> bool:
        """删除模型

        Args:
            model_id: 模型记录 ID

        Returns:
            是否删除成功
        """
        existing = await self.get_model(model_id)
        if existing is None:
            return False

        await self.db.execute("DELETE FROM models WHERE id = ?", (model_id,))
        await self.db.commit()
        logger.info(f"模型已删除: {model_id}")
        return True

    # ================================================================
    # Tasks（任务）CRUD
    # ================================================================

    async def create_task(
        self,
        task_id: str,
        name: str,
        status: str = "pending",
        agent: str = "",
        created_at: Optional[str] = None,
        completed_at: Optional[str] = None,
        description: str = "",
        result: Optional[str] = None,
    ) -> dict:
        """创建新任务

        Args:
            task_id: 任务 ID
            name: 任务名称
            status: 任务状态（pending/running/completed/failed）
            agent: 执行代理名称
            created_at: 创建时间
            completed_at: 完成时间
            description: 任务描述
            result: 执行结果

        Returns:
            创建的任务字典
        """
        created_at = created_at or time.strftime("%Y-%m-%dT%H:%M:%S")

        await self.db.execute(
            "INSERT INTO tasks (id, name, status, agent, created_at, completed_at, description, result) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, name, status, agent, created_at, completed_at, description, result),
        )
        await self.db.commit()
        logger.info(f"任务已创建: {name}")

        return {
            "id": task_id,
            "name": name,
            "status": status,
            "agent": agent,
            "created_at": created_at,
            "completed_at": completed_at,
            "description": description,
            "result": result,
            "steps": [],
        }

    async def list_tasks(self, status: Optional[str] = None) -> list[dict]:
        """获取任务列表，支持按状态过滤

        Args:
            status: 过滤状态，为 None 或 "all" 时返回全部

        Returns:
            任务字典列表（含步骤）
        """
        if status and status != "all":
            cursor = await self.db.execute(
                "SELECT id, name, status, agent, created_at, completed_at, description, result "
                "FROM tasks WHERE status = ? ORDER BY created_at DESC",
                (status,),
            )
        else:
            cursor = await self.db.execute(
                "SELECT id, name, status, agent, created_at, completed_at, description, result "
                "FROM tasks ORDER BY created_at DESC"
            )

        rows = await cursor.fetchall()
        tasks = []
        task_ids = []
        for row in rows:
            task = {
                "id": row[0],
                "name": row[1],
                "status": row[2],
                "agent": row[3],
                "created_at": row[4],
                "completed_at": row[5],
                "description": row[6],
                "result": row[7],
                "steps": [],
            }
            tasks.append(task)
            task_ids.append(row[0])

        # Batch query all steps at once to avoid N+1
        if task_ids:
            placeholders = ",".join("?" for _ in task_ids)
            cursor = await self.db.execute(
                f"SELECT id, task_id, name, status, duration FROM task_steps "
                f"WHERE task_id IN ({placeholders}) ORDER BY id ASC",
                task_ids,
            )
            steps = await cursor.fetchall()
            steps_by_task: dict[str, list[dict]] = {}
            for step in steps:
                step_dict = {
                    "id": step[0],
                    "task_id": step[1],
                    "name": step[2],
                    "status": step[3],
                    "duration": step[4],
                }
                steps_by_task.setdefault(step[1], []).append(step_dict)
            for task in tasks:
                task["steps"] = steps_by_task.get(task["id"], [])

        return tasks

    async def get_task(self, task_id: str) -> Optional[dict]:
        """根据 ID 获取任务详情（含步骤）

        Args:
            task_id: 任务 ID

        Returns:
            任务字典，不存在则返回 None
        """
        cursor = await self.db.execute(
            "SELECT id, name, status, agent, created_at, completed_at, description, result "
            "FROM tasks WHERE id = ?",
            (task_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None

        task = {
            "id": row[0],
            "name": row[1],
            "status": row[2],
            "agent": row[3],
            "created_at": row[4],
            "completed_at": row[5],
            "description": row[6],
            "result": row[7],
        }
        task["steps"] = await self.list_task_steps(task_id)
        return task

    async def update_task_status(
        self, task_id: str, status: str, result: Optional[str] = None
    ) -> Optional[dict]:
        """更新任务状态

        Args:
            task_id: 任务 ID
            status: 新状态
            result: 执行结果（可选）

        Returns:
            更新后的任务字典，不存在则返回 None
        """
        existing = await self.get_task(task_id)
        if existing is None:
            return None

        completed_at = time.strftime("%Y-%m-%dT%H:%M:%S") if status in ("completed", "failed") else None

        if result is not None:
            await self.db.execute(
                "UPDATE tasks SET status = ?, completed_at = ?, result = ? WHERE id = ?",
                (status, completed_at, result, task_id),
            )
        else:
            await self.db.execute(
                "UPDATE tasks SET status = ?, completed_at = ? WHERE id = ?",
                (status, completed_at, task_id),
            )
        await self.db.commit()

        return await self.get_task(task_id)

    # ================================================================
    # TaskSteps（任务步骤）CRUD
    # ================================================================

    async def create_task_step(
        self,
        task_id: str,
        name: str,
        status: str = "pending",
        duration: Optional[str] = None,
    ) -> dict:
        """创建任务步骤

        Args:
            task_id: 所属任务 ID
            name: 步骤名称
            status: 步骤状态
            duration: 耗时

        Returns:
            创建的步骤字典
        """
        cursor = await self.db.execute(
            "INSERT INTO task_steps (task_id, name, status, duration) VALUES (?, ?, ?, ?)",
            (task_id, name, status, duration),
        )
        await self.db.commit()

        step_id = cursor.lastrowid
        return {
            "id": step_id,
            "task_id": task_id,
            "name": name,
            "status": status,
            "duration": duration,
        }

    async def list_task_steps(self, task_id: str) -> list[dict]:
        """获取指定任务的步骤列表

        Args:
            task_id: 任务 ID

        Returns:
            步骤字典列表
        """
        cursor = await self.db.execute(
            "SELECT id, task_id, name, status, duration FROM task_steps WHERE task_id = ? ORDER BY id ASC",
            (task_id,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "task_id": row[1],
                "name": row[2],
                "status": row[3],
                "duration": row[4],
            }
            for row in rows
        ]

    async def update_task_step_status(
        self, step_id: int, status: str, duration: Optional[str] = None
    ) -> Optional[dict]:
        """更新任务步骤状态

        Args:
            step_id: 步骤 ID
            status: 新状态
            duration: 耗时（可选）

        Returns:
            更新后的步骤字典，不存在则返回 None
        """
        cursor = await self.db.execute(
            "SELECT id, task_id, name, status, duration FROM task_steps WHERE id = ?",
            (step_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None

        if duration is not None:
            await self.db.execute(
                "UPDATE task_steps SET status = ?, duration = ? WHERE id = ?",
                (status, duration, step_id),
            )
        else:
            await self.db.execute(
                "UPDATE task_steps SET status = ? WHERE id = ?",
                (status, step_id),
            )
        await self.db.commit()

        # 重新查询返回
        cursor = await self.db.execute(
            "SELECT id, task_id, name, status, duration FROM task_steps WHERE id = ?",
            (step_id,),
        )
        row = await cursor.fetchone()
        return {
            "id": row[0],
            "task_id": row[1],
            "name": row[2],
            "status": row[3],
            "duration": row[4],
        }

    # ================================================================
    # Memories（记忆）CRUD
    # ================================================================

    async def create_memory(
        self,
        memory_id: str,
        title: str,
        content: str,
        tags: Optional[list[str]] = None,
        source: str = "chat",
        importance: float = 0.5,
        created_at: Optional[str] = None,
        access_count: int = 0,
    ) -> dict:
        """创建新记忆

        Args:
            memory_id: 记忆 ID
            title: 标题
            content: 内容
            tags: 标签列表
            source: 来源（chat/system/import）
            importance: 重要度（0.0-1.0）
            created_at: 创建时间
            access_count: 访问计数

        Returns:
            创建的记忆字典
        """
        tags = tags or []
        created_at = created_at or time.strftime("%Y-%m-%dT%H:%M:%S")

        await self.db.execute(
            "INSERT INTO memories (id, title, content, tags, source, importance, created_at, access_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (memory_id, title, content, json.dumps(tags, ensure_ascii=False), source, importance, created_at, access_count),
        )
        await self.db.commit()
        logger.info(f"记忆已创建: {title}")

        return {
            "id": memory_id,
            "title": title,
            "content": content,
            "tags": tags,
            "source": source,
            "importance": importance,
            "created_at": created_at,
            "access_count": access_count,
        }

    async def list_memories(self) -> list[dict]:
        """获取所有记忆列表

        Returns:
            记忆字典列表
        """
        cursor = await self.db.execute(
            "SELECT id, title, content, tags, source, importance, created_at, access_count FROM memories "
            "ORDER BY importance DESC"
        )
        rows = await cursor.fetchall()
        return [self._row_to_memory(row) for row in rows]

    async def get_memory(self, memory_id: str) -> Optional[dict]:
        """根据 ID 获取记忆

        Args:
            memory_id: 记忆 ID

        Returns:
            记忆字典，不存在则返回 None
        """
        cursor = await self.db.execute(
            "SELECT id, title, content, tags, source, importance, created_at, access_count "
            "FROM memories WHERE id = ?",
            (memory_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_memory(row)

    async def search_memories(self, keyword: str) -> list[dict]:
        """搜索记忆（关键词匹配标题、内容、标签）

        使用 LIKE 模糊匹配，并按相关度 * 重要度排序。

        Args:
            keyword: 搜索关键词

        Returns:
            匹配的记忆字典列表（含 relevance 字段）
        """
        if not keyword:
            return await self.list_memories()

        pattern = f"%{keyword}%"
        cursor = await self.db.execute(
            "SELECT id, title, content, tags, source, importance, created_at, access_count "
            "FROM memories WHERE title LIKE ? OR content LIKE ? OR tags LIKE ?",
            (pattern, pattern, pattern),
        )
        rows = await cursor.fetchall()

        # 计算相关度分数
        results = []
        keyword_lower = keyword.lower()
        for row in rows:
            mem = self._row_to_memory(row)
            score = 0.0
            if keyword_lower in mem["title"].lower():
                score += 0.5
            if keyword_lower in mem["content"].lower():
                score += 0.3
            for tag in mem.get("tags", []):
                if keyword_lower in tag.lower():
                    score += 0.2
            if score > 0:
                mem["relevance"] = round(score * mem.get("importance", 0.5), 3)
                results.append(mem)

        results.sort(key=lambda x: x["relevance"], reverse=True)
        return results

    async def update_memory(self, memory_id: str, **kwargs) -> Optional[dict]:
        """更新记忆

        Args:
            memory_id: 记忆 ID
            **kwargs: 可更新字段（title, content, tags, source, importance, access_count）

        Returns:
            更新后的记忆字典，不存在则返回 None
        """
        existing = await self.get_memory(memory_id)
        if existing is None:
            return None

        allowed_fields = {"title", "content", "tags", "source", "importance", "access_count"}
        updates = {}
        for k, v in kwargs.items():
            if k in allowed_fields and v is not None:
                # tags 需要序列化为 JSON 字符串
                if k == "tags" and isinstance(v, list):
                    updates[k] = json.dumps(v, ensure_ascii=False)
                else:
                    updates[k] = v

        if not updates:
            return existing

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [memory_id]

        await self.db.execute(f"UPDATE memories SET {set_clause} WHERE id = ?", values)
        await self.db.commit()

        return await self.get_memory(memory_id)

    async def delete_memory(self, memory_id: str) -> bool:
        """删除记忆

        Args:
            memory_id: 记忆 ID

        Returns:
            是否删除成功
        """
        existing = await self.get_memory(memory_id)
        if existing is None:
            return False

        await self.db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        await self.db.commit()
        logger.info(f"记忆已删除: {memory_id}")
        return True

    def _row_to_memory(self, row: tuple) -> dict:
        """将数据库行转换为记忆字典

        Args:
            row: 数据库查询结果行

        Returns:
            记忆字典
        """
        return {
            "id": row[0],
            "title": row[1],
            "content": row[2],
            "tags": json.loads(row[3]) if row[3] else [],
            "source": row[4],
            "importance": row[5],
            "created_at": row[6],
            "access_count": row[7],
        }

    # ================================================================
    # Skills（技能）CRUD
    # ================================================================

    async def create_skill(
        self,
        skill_id: str,
        name: str,
        description: str = "",
        version: str = "1.0.0",
        source: str = "builtin",
        enabled: bool = True,
        trigger_keywords: Optional[list[str]] = None,
        created_at: Optional[str] = None,
    ) -> dict:
        """创建新技能

        Args:
            skill_id: 技能 ID
            name: 技能名称
            description: 技能描述
            version: 版本号
            source: 来源（builtin/custom/external/imported）
            enabled: 是否启用
            trigger_keywords: 触发关键词列表
            created_at: 创建时间

        Returns:
            创建的技能字典
        """
        trigger_keywords = trigger_keywords or []
        created_at = created_at or time.strftime("%Y-%m-%dT%H:%M:%S")

        await self.db.execute(
            "INSERT INTO skills (id, name, description, version, source, enabled, trigger_keywords, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (skill_id, name, description, version, source, enabled,
             json.dumps(trigger_keywords, ensure_ascii=False), created_at),
        )
        await self.db.commit()
        logger.info(f"技能已创建: {name}")

        return {
            "id": skill_id,
            "name": name,
            "description": description,
            "version": version,
            "source": source,
            "enabled": enabled,
            "trigger_keywords": trigger_keywords,
            "created_at": created_at,
        }

    async def list_skills(self) -> list[dict]:
        """获取所有技能列表

        Returns:
            技能字典列表
        """
        cursor = await self.db.execute(
            "SELECT id, name, description, version, source, enabled, trigger_keywords, created_at "
            "FROM skills ORDER BY created_at ASC"
        )
        rows = await cursor.fetchall()
        return [self._row_to_skill(row) for row in rows]

    async def get_skill(self, skill_id: str) -> Optional[dict]:
        """根据 ID 获取技能

        Args:
            skill_id: 技能 ID

        Returns:
            技能字典，不存在则返回 None
        """
        cursor = await self.db.execute(
            "SELECT id, name, description, version, source, enabled, trigger_keywords, created_at "
            "FROM skills WHERE id = ?",
            (skill_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_skill(row)

    async def search_skills(self, keyword: str) -> list[dict]:
        """搜索技能（关键词匹配名称、描述、触发关键词）

        Args:
            keyword: 搜索关键词

        Returns:
            匹配的技能字典列表（含 relevance 字段）
        """
        if not keyword:
            return await self.list_skills()

        pattern = f"%{keyword}%"
        cursor = await self.db.execute(
            "SELECT id, name, description, version, source, enabled, trigger_keywords, created_at "
            "FROM skills WHERE name LIKE ? OR description LIKE ? OR trigger_keywords LIKE ?",
            (pattern, pattern, pattern),
        )
        rows = await cursor.fetchall()

        results = []
        keyword_lower = keyword.lower()
        for row in rows:
            sk = self._row_to_skill(row)
            score = 0.0
            if keyword_lower in sk["name"].lower():
                score += 0.5
            if keyword_lower in sk.get("description", "").lower():
                score += 0.3
            for kw in sk.get("trigger_keywords", []):
                if keyword_lower in kw.lower():
                    score += 0.2
            if score > 0:
                sk["relevance"] = round(score, 3)
                results.append(sk)

        results.sort(key=lambda x: x["relevance"], reverse=True)
        return results

    async def update_skill(self, skill_id: str, **kwargs) -> Optional[dict]:
        """更新技能

        Args:
            skill_id: 技能 ID
            **kwargs: 可更新字段（name, description, version, enabled, trigger_keywords）

        Returns:
            更新后的技能字典，不存在则返回 None
        """
        existing = await self.get_skill(skill_id)
        if existing is None:
            return None

        allowed_fields = {"name", "description", "version", "enabled", "trigger_keywords"}
        updates = {}
        for k, v in kwargs.items():
            if k in allowed_fields and v is not None:
                if k == "trigger_keywords" and isinstance(v, list):
                    updates[k] = json.dumps(v, ensure_ascii=False)
                else:
                    updates[k] = v

        if not updates:
            return existing

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [skill_id]

        await self.db.execute(f"UPDATE skills SET {set_clause} WHERE id = ?", values)
        await self.db.commit()

        return await self.get_skill(skill_id)

    async def delete_skill(self, skill_id: str) -> bool:
        """删除技能

        Args:
            skill_id: 技能 ID

        Returns:
            是否删除成功
        """
        existing = await self.get_skill(skill_id)
        if existing is None:
            return False

        await self.db.execute("DELETE FROM skills WHERE id = ?", (skill_id,))
        await self.db.commit()
        logger.info(f"技能已删除: {skill_id}")
        return True

    def _row_to_skill(self, row: tuple) -> dict:
        """将数据库行转换为技能字典

        Args:
            row: 数据库查询结果行

        Returns:
            技能字典
        """
        return {
            "id": row[0],
            "name": row[1],
            "description": row[2],
            "version": row[3],
            "source": row[4],
            "enabled": bool(row[5]),
            "trigger_keywords": json.loads(row[6]) if row[6] else [],
            "created_at": row[7],
        }

    # ================================================================
    # Config（配置）CRUD
    # ================================================================

    async def get_config(self, key: str, default: Any = None) -> Any:
        """获取配置值

        Args:
            key: 配置键
            default: 默认值

        Returns:
            配置值（自动反序列化 JSON），不存在则返回默认值
        """
        cursor = await self.db.execute("SELECT value FROM config WHERE key = ?", (key,))
        row = await cursor.fetchone()
        if row is None:
            return default

        # 尝试 JSON 反序列化
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return row[0]

    async def set_config(self, key: str, value: Any) -> None:
        """设置配置值

        Args:
            key: 配置键
            value: 配置值（自动序列化为 JSON 存储）
        """
        # 序列化为 JSON 字符串
        if isinstance(value, (dict, list)):
            serialized = json.dumps(value, ensure_ascii=False)
        else:
            serialized = str(value)

        await self.db.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            (key, serialized),
        )
        await self.db.commit()
        logger.debug(f"配置已设置: {key}")

    async def get_all_config(self) -> dict:
        """获取所有配置

        Returns:
            配置字典
        """
        cursor = await self.db.execute("SELECT key, value FROM config")
        rows = await cursor.fetchall()

        result = {}
        for row in rows:
            try:
                result[row[0]] = json.loads(row[1])
            except (json.JSONDecodeError, TypeError):
                result[row[0]] = row[1]

        return result


async def get_db(db_path: str = DEFAULT_DB_PATH) -> Database:
    """获取数据库单例实例

    首次调用时创建实例并连接数据库，后续调用返回已有实例。
    如果数据库路径发生变化，会重新连接。

    Args:
        db_path: 数据库文件路径，默认 ./data/symbio.db

    Returns:
        Database 实例
    """
    global _db_instance

    if _db_instance is None:
        _db_instance = Database(db_path)
        await _db_instance.connect()
        logger.info("数据库单例已初始化")
    elif _db_instance.db_path != db_path:
        # 路径变化，重新连接
        await _db_instance.close()
        _db_instance = Database(db_path)
        await _db_instance.connect()
        logger.info(f"数据库已重连至: {db_path}")

    return _db_instance


async def close_db() -> None:
    """关闭全局数据库连接"""
    global _db_instance
    if _db_instance is not None:
        await _db_instance.close()
        _db_instance = None
