"""StateManager 快照续传与保留策略测试。

原先 core/checkpoint.py 另起一张 checkpoints 表做同样的事（保存/加载/列出/清理），
但没有任何调用方。这里把它真正独有的两个语义补在 StateManager 上并锁住：
按版本回滚，以及快照保留清理（否则 state_snapshots 表会无界增长）。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.core.state_manager import StateManager, TaskPhase


async def _seeded(db_path, task_id="task-1", updates=3):
    manager = StateManager(persist_path=str(db_path))
    await manager.initialize(task_id, "Build the feature")
    for i in range(updates):
        await manager.update(
            lambda s, i=i: s.model_copy(update={"metadata": {**s.metadata, "step": f"step-{i}"}})
        )
    return manager


async def test_list_snapshots_tracks_every_version(tmp_path):
    manager = await _seeded(tmp_path / "state.db")
    try:
        snapshots = await manager.list_snapshots("task-1")
        versions = [s["version"] for s in snapshots]
        # 初始化 + 3 次 update，版本号递减排列
        assert versions == sorted(versions, reverse=True)
        assert len(versions) >= 4
    finally:
        await manager.close()


async def test_restore_version_returns_that_exact_snapshot(tmp_path):
    db_path = tmp_path / "state.db"
    manager = await _seeded(db_path)
    snapshots = await manager.list_snapshots("task-1")
    oldest = min(s["version"] for s in snapshots)
    latest = max(s["version"] for s in snapshots)
    await manager.close()

    reopened = StateManager(persist_path=str(db_path))
    try:
        old_state = await reopened.restore_version("task-1", oldest)
        assert old_state is not None
        assert old_state.version == oldest

        newest = await reopened.restore("task-1")
        assert newest is not None
        assert newest.version == latest
        assert newest.metadata["step"] == "step-2"
    finally:
        await reopened.close()


async def test_restore_version_missing_returns_none(tmp_path):
    manager = await _seeded(tmp_path / "state.db")
    try:
        assert await manager.restore_version("task-1", 9999) is None
        assert await manager.restore_version("no-such-task", 1) is None
    finally:
        await manager.close()


async def test_cleanup_keep_last_prunes_history(tmp_path):
    manager = await _seeded(tmp_path / "state.db", updates=6)
    try:
        before = len(await manager.list_snapshots("task-1", limit=100))
        assert before >= 7

        deleted = await manager.cleanup_old_snapshots(keep_last=2)
        assert deleted == before - 2

        remaining = await manager.list_snapshots("task-1", limit=100)
        assert len(remaining) == 2
        # 保留的必须是最新的两个，续传能力不受影响
        assert await manager.restore("task-1") is not None
    finally:
        await manager.close()


async def test_cleanup_keep_last_is_per_task(tmp_path):
    db_path = tmp_path / "state.db"
    manager = await _seeded(db_path, task_id="task-a", updates=4)
    await manager.close()

    manager_b = await _seeded(db_path, task_id="task-b", updates=4)
    try:
        await manager_b.cleanup_old_snapshots(keep_last=1)
        assert len(await manager_b.list_snapshots("task-a", limit=100)) == 1
        assert len(await manager_b.list_snapshots("task-b", limit=100)) == 1
    finally:
        await manager_b.close()


async def test_cleanup_by_days_only_removes_old_rows(tmp_path):
    manager = await _seeded(tmp_path / "state.db", updates=2)
    try:
        # 手工把一条快照改成 60 天前
        old_ts = (datetime.now() - timedelta(days=60)).isoformat()
        await manager._db.execute(
            "UPDATE state_snapshots SET created_at = ? WHERE task_id = ? AND version = 1",
            (old_ts, "task-1"),
        )
        await manager._db.commit()

        before = len(await manager.list_snapshots("task-1", limit=100))
        deleted = await manager.cleanup_old_snapshots(days=30)

        assert deleted == 1
        assert len(await manager.list_snapshots("task-1", limit=100)) == before - 1
    finally:
        await manager.close()


async def test_cleanup_requires_a_retention_policy(tmp_path):
    manager = await _seeded(tmp_path / "state.db", updates=1)
    try:
        with pytest.raises(ValueError):
            await manager.cleanup_old_snapshots()
        with pytest.raises(ValueError):
            await manager.cleanup_old_snapshots(keep_last=0)
    finally:
        await manager.close()


async def test_cleanup_scoped_to_single_task(tmp_path):
    db_path = tmp_path / "state.db"
    manager = await _seeded(db_path, task_id="task-a", updates=3)
    await manager.close()

    manager_b = await _seeded(db_path, task_id="task-b", updates=3)
    try:
        await manager_b.cleanup_old_snapshots(keep_last=1, task_id="task-a")
        assert len(await manager_b.list_snapshots("task-a", limit=100)) == 1
        assert len(await manager_b.list_snapshots("task-b", limit=100)) == 4
    finally:
        await manager_b.close()


async def test_phase_updates_are_snapshotted(tmp_path):
    """阶段变更也要进快照，续传后能知道任务停在哪个阶段。"""
    db_path = tmp_path / "state.db"
    manager = StateManager(persist_path=str(db_path))
    await manager.initialize("task-1", "Build the feature")
    await manager.update(lambda s: s.model_copy(update={"phase": TaskPhase.EXECUTING}))
    await manager.close()

    reopened = StateManager(persist_path=str(db_path))
    try:
        restored = await reopened.restore("task-1")
        assert restored is not None
        assert restored.phase == TaskPhase.EXECUTING
    finally:
        await reopened.close()
