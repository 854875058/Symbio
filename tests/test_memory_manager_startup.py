"""MemoryManager startup degradation tests."""

import builtins
import sys
from datetime import datetime, timedelta

import pytest

from symbio.memory.manager import MemoryManager, MemoryManagerConfig


@pytest.mark.asyncio
async def test_initialize_degrades_to_memory_mode_when_vector_backend_import_breaks(
    monkeypatch, tmp_path
):
    """Optional vector backend ABI/import failures should not break API startup."""
    real_import = builtins.__import__

    def fail_lancedb_import(name, *args, **kwargs):
        if name == "lancedb":
            raise AttributeError("_ARRAY_API not found")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_lancedb_import)

    manager = MemoryManager(
        MemoryManagerConfig(
            lancedb_path=str(tmp_path / "memory"),
            enable_decay=False,
        )
    )

    await manager.initialize()

    assert manager._initialized is True
    assert manager._db is None
    assert manager._table is None


@pytest.mark.asyncio
async def test_initialize_suppresses_optional_backend_stderr_noise(monkeypatch, tmp_path, capsys):
    """Noisy optional backend import failures should not flood server startup stderr."""
    real_import = builtins.__import__

    def noisy_lancedb_import(name, *args, **kwargs):
        if name == "lancedb":
            print("RAW OPTIONAL BACKEND TRACEBACK", file=sys.stderr)
            raise AttributeError("_ARRAY_API not found")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", noisy_lancedb_import)

    manager = MemoryManager(
        MemoryManagerConfig(
            lancedb_path=str(tmp_path / "memory"),
            enable_decay=False,
        )
    )

    await manager.initialize()

    captured = capsys.readouterr()
    assert "RAW OPTIONAL BACKEND TRACEBACK" not in captured.err


async def _manager(tmp_path):
    manager = MemoryManager(
        MemoryManagerConfig(
            lancedb_path=str(tmp_path / "memory"),
            enable_decay=False,
        )
    )
    await manager.initialize()
    return manager


@pytest.mark.asyncio
async def test_first_memory_is_persisted_to_empty_table(tmp_path):
    """空表时写入第一条记忆必须落盘。

    LanceTable 实现了 __len__ 而没有 __bool__，空表布尔值为 False；历史上
    ``if self._table:`` 让第一条记忆永远写不进去，此后表恒为空，持久化整体静默失效。
    """
    manager = await _manager(tmp_path)
    try:
        assert manager._table is not None
        assert bool(manager._table) is False, "前提：空表布尔值为 False"
        await manager.add_memory("第一条记忆")
        assert manager._table.count_rows() == 1
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_memories_survive_restart(tmp_path):
    manager = await _manager(tmp_path)
    try:
        await manager.add_memory("需要跨重启保留的记忆", tags=["persist"])
        await manager.add_memory("第二条记忆")
    finally:
        await manager.close()

    reopened = await _manager(tmp_path)
    try:
        restored = {**reopened._short_term, **reopened._long_term}
        assert len(restored) == 2
        assert any(m.content == "需要跨重启保留的记忆" for m in restored.values())
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_update_memory_does_not_duplicate_rows(tmp_path):
    """_update_memory 走删除+重插，行数不应增长。"""
    manager = await _manager(tmp_path)
    try:
        memory = await manager.add_memory("会被更新的记忆")
        memory.importance = 0.95
        await manager._update_memory(memory)

        assert manager._table.count_rows() == 1
        rows = manager._table.search(None).limit(0).to_list()
        assert rows[0]["importance"] == pytest.approx(0.95)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_update_memory_with_quote_in_id(tmp_path):
    """memory_id 含单引号时 where 子句不应语法出错，导致更新变成重复插入。"""
    manager = await _manager(tmp_path)
    try:
        memory = await manager.add_memory("带引号 ID 的记忆")
        memory.memory_id = "it's-an-id"
        await manager._persist_memory(memory)
        assert manager._table.count_rows() == 2

        memory.importance = 0.9
        await manager._update_memory(memory)
        assert manager._table.count_rows() == 2
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_expired_memory_does_not_resurrect_after_restart(tmp_path):
    """被遗忘的记忆必须同时从磁盘删除，否则重启时会被恢复回来。"""
    manager = await _manager(tmp_path)
    try:
        keep = await manager.add_memory("保留的记忆")
        expired = await manager.add_memory("已过期的记忆")
        expired.expires_at = datetime.now() - timedelta(hours=1)

        await manager._apply_decay()
        assert expired.memory_id not in manager._short_term
        assert manager._table.count_rows() == 1
    finally:
        await manager.close()

    reopened = await _manager(tmp_path)
    try:
        restored = {**reopened._short_term, **reopened._long_term}
        assert list(restored) == [keep.memory_id]
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_short_term_window_eviction_removes_persisted_rows(tmp_path):
    """短期窗口溢出淘汰的记忆不应留在磁盘上。"""
    manager = MemoryManager(
        MemoryManagerConfig(
            lancedb_path=str(tmp_path / "memory"),
            enable_decay=False,
            short_term_window=3,
        )
    )
    await manager.initialize()
    try:
        for i in range(6):
            await manager.add_memory(f"记忆 {i}", importance=0.1 * (i + 1))

        assert len(manager._short_term) == 3
        assert manager._table.count_rows() == 3
    finally:
        await manager.close()

    reopened = MemoryManager(
        MemoryManagerConfig(
            lancedb_path=str(tmp_path / "memory"),
            enable_decay=False,
            short_term_window=3,
        )
    )
    await reopened.initialize()
    try:
        assert len(reopened._short_term) == 3
    finally:
        await reopened.close()
