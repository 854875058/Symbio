"""项目级记忆隔离 + 跨项目知识迁移测试。

重点覆盖持久化路径：项目元数据、记忆、迁移副本都必须跨重启可见。历史上
``if self._table:`` 这类真值判断在空表时为 False，导致第一条记忆永远写不进去，
持久化整体静默失效 —— 这些用例通过"重建 manager 再读"来锁住该行为。
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.memory import ProjectMemoryManager


async def _manager(tmp_path):
    manager = ProjectMemoryManager(storage_dir=str(tmp_path))
    await manager.initialize()
    return manager


@pytest.mark.asyncio
async def test_project_and_memory_survive_restart(tmp_path):
    manager = await _manager(tmp_path)
    try:
        await manager.create_project_async("demo", project_name="Demo")
        await manager.add_memory("demo", "使用 pytest 做单元测试", tags=["testing"])
        await manager.add_memory("demo", "部署走 docker compose", tags=["ops"])
    finally:
        await manager.close()

    reopened = await _manager(tmp_path)
    try:
        projects = {p.project_id: p for p in reopened.list_projects()}
        assert "demo" in projects
        assert projects["demo"].project_name == "Demo"
        assert len(await reopened.list_memories("demo")) == 2
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_first_memory_is_persisted(tmp_path):
    """空表时写入第一条记忆必须落盘（LanceTable 空表布尔值为 False 的回归）。"""
    manager = await _manager(tmp_path)
    try:
        await manager.add_memory("solo", "唯一的一条记忆")
        assert manager._table.count_rows() == 1
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_empty_project_visible_after_restart(tmp_path):
    """没有任何记忆的项目也要能在重启后列出。"""
    manager = await _manager(tmp_path)
    try:
        await manager.create_project_async("empty", project_name="Empty")
    finally:
        await manager.close()

    reopened = await _manager(tmp_path)
    try:
        assert [p.project_id for p in reopened.list_projects()] == ["empty"]
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_project_isolation(tmp_path):
    manager = await _manager(tmp_path)
    try:
        await manager.add_memory("proj-a", "A 项目的记忆")
        await manager.add_memory("proj-b", "B 项目的记忆")

        assert len(await manager.list_memories("proj-a")) == 1
        assert len(await manager.list_memories("proj-b")) == 1
        # 限定项目检索不应跨项目命中
        hits = await manager.search("项目的记忆", project_id="proj-a", similarity_threshold=0.1)
        assert {h["project_id"] for h in hits} == {"proj-a"}
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_search_uses_local_embedding_without_api_key(tmp_path):
    """无 API Key 时应走本地降级 embedding，而不是退化成空向量。"""
    manager = await _manager(tmp_path)
    try:
        item = await manager.add_memory("demo", "使用 pytest 做单元测试")
        assert item.embedding, "本地降级 embedding 不应为空"
        assert len(item.embedding) == manager._local_embedder.dim

        hits = await manager.search("单元测试怎么做", project_id="demo", similarity_threshold=0.3)
        assert len(hits) == 1
        assert hits[0]["similarity"] > 0.3
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_transfer_knowledge_persists_copy(tmp_path):
    manager = await _manager(tmp_path)
    try:
        await manager.create_project_async("src-proj")
        await manager.create_project_async("dst-proj")
        item = await manager.add_memory("src-proj", "可复用的架构决策")

        record = await manager.transfer_knowledge(
            "src-proj", "dst-proj", [item.memory_id], reason="reuse"
        )
        assert record.memory_ids == [item.memory_id]
        assert len(await manager.list_memories("dst-proj")) == 1
    finally:
        await manager.close()

    reopened = await _manager(tmp_path)
    try:
        # 迁移产生的副本同样要跨重启存在
        assert len(await reopened.list_memories("dst-proj")) == 1
        assert len(await reopened.list_memories("src-proj")) == 1
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_delete_memory_removes_persisted_row(tmp_path):
    manager = await _manager(tmp_path)
    try:
        item = await manager.add_memory("demo", "待删除的记忆")
        assert await manager.delete_memory("demo", item.memory_id) is True
        assert manager._table.count_rows() == 0
    finally:
        await manager.close()

    reopened = await _manager(tmp_path)
    try:
        assert await reopened.list_memories("demo") == []
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_delete_project_async_clears_persistence(tmp_path):
    manager = await _manager(tmp_path)
    try:
        await manager.create_project_async("gone", project_name="Gone")
        await manager.add_memory("gone", "随项目一起删除")
        assert await manager.delete_project_async("gone") is True
    finally:
        await manager.close()

    reopened = await _manager(tmp_path)
    try:
        assert reopened.list_projects() == []
        assert await reopened.list_memories("gone") == []
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_quote_in_project_id_does_not_break_delete(tmp_path):
    manager = await _manager(tmp_path)
    try:
        await manager.create_project_async("it's-a-project")
        await manager.add_memory("it's-a-project", "带引号的项目 ID")
        assert await manager.delete_project_async("it's-a-project") is True
        assert manager._table.count_rows() == 0
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_statistics_reflect_state(tmp_path):
    manager = await _manager(tmp_path)
    try:
        await manager.create_project_async("p1")
        await manager.add_memory("p1", "记忆一")
        await manager.add_memory("p1", "记忆二", memory_type="episodic")

        stats = manager.get_statistics()
        assert stats["total_projects"] == 1
        assert stats["total_memories"] == 2
        assert stats["projects"]["p1"]["by_type"]["episodic"] == 1
    finally:
        await manager.close()
