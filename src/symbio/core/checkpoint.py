"""状态持久化与断点续传 - 基于事件溯源的检查点管理"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import aiosqlite

from symbio.utils.logger import get_logger

logger = get_logger("checkpoint")


class CheckpointManager:
    """检查点管理器

    负责任务状态的持久化和断点续传。
    """

    def __init__(self, db_path: str = "./data/checkpoints.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db: Optional[aiosqlite.Connection] = None

    async def initialize(self) -> None:
        """初始化数据库"""
        self._db = await aiosqlite.connect(str(self.db_path))
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS checkpoints (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                data TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_task_id ON checkpoints(task_id)
        """)
        await self._db.commit()
        logger.info(f"检查点数据库初始化完成: {self.db_path}")

    async def close(self) -> None:
        """关闭数据库连接"""
        if self._db:
            await self._db.close()
            self._db = None

    async def save_checkpoint(
        self,
        task_id: str,
        state: dict[str, Any],
    ) -> str:
        """保存检查点

        Args:
            task_id: 任务 ID
            state: 任务状态

        Returns:
            检查点 ID
        """
        if not self._db:
            await self.initialize()

        checkpoint_id = str(uuid4())
        data = json.dumps(state, ensure_ascii=False, default=str)
        created_at = datetime.now().isoformat()

        await self._db.execute(
            "INSERT INTO checkpoints (id, task_id, data, created_at) VALUES (?, ?, ?, ?)",
            (checkpoint_id, task_id, data, created_at),
        )
        await self._db.commit()

        logger.debug(f"保存检查点: {checkpoint_id}, task={task_id}")
        return checkpoint_id

    async def load_checkpoint(self, checkpoint_id: str) -> Optional[dict]:
        """加载检查点

        Args:
            checkpoint_id: 检查点 ID

        Returns:
            任务状态，如果不存在返回 None
        """
        if not self._db:
            await self.initialize()

        cursor = await self._db.execute(
            "SELECT data FROM checkpoints WHERE id = ?", (checkpoint_id,)
        )
        row = await cursor.fetchone()

        if not row:
            logger.warning(f"检查点不存在: {checkpoint_id}")
            return None

        return json.loads(row[0])

    async def get_latest_checkpoint(self, task_id: str) -> Optional[dict]:
        """获取任务的最新检查点

        Args:
            task_id: 任务 ID

        Returns:
            任务状态，如果不存在返回 None
        """
        if not self._db:
            await self.initialize()

        cursor = await self._db.execute(
            "SELECT data FROM checkpoints WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        )
        row = await cursor.fetchone()

        if not row:
            return None

        return json.loads(row[0])

    async def list_checkpoints(
        self,
        task_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """列出检查点

        Args:
            task_id: 任务 ID（可选）
            limit: 最大返回数量

        Returns:
            检查点列表
        """
        if not self._db:
            await self.initialize()

        if task_id:
            cursor = await self._db.execute(
                "SELECT id, task_id, created_at FROM checkpoints WHERE task_id = ? ORDER BY created_at DESC LIMIT ?",
                (task_id, limit),
            )
        else:
            cursor = await self._db.execute(
                "SELECT id, task_id, created_at FROM checkpoints ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )

        rows = await cursor.fetchall()
        return [{"id": row[0], "task_id": row[1], "created_at": row[2]} for row in rows]

    async def delete_checkpoint(self, checkpoint_id: str) -> bool:
        """删除检查点

        Args:
            checkpoint_id: 检查点 ID

        Returns:
            是否删除成功
        """
        if not self._db:
            await self.initialize()

        cursor = await self._db.execute("DELETE FROM checkpoints WHERE id = ?", (checkpoint_id,))
        await self._db.commit()

        return cursor.rowcount > 0

    async def cleanup_old_checkpoints(self, days: int = 30) -> int:
        """清理旧检查点

        Args:
            days: 保留天数

        Returns:
            删除数量
        """
        if not self._db:
            await self.initialize()

        cutoff = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        from datetime import timedelta

        cutoff = cutoff - timedelta(days=days)

        cursor = await self._db.execute(
            "DELETE FROM checkpoints WHERE created_at < ?", (cutoff.isoformat(),)
        )
        await self._db.commit()

        deleted = cursor.rowcount
        if deleted > 0:
            logger.info(f"清理旧检查点: {deleted} 个")

        return deleted
