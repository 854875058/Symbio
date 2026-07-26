"""语义缓存维护路径回归测试。

这些路径（LRU 淘汰、命中计数落盘、条目枚举、Prompt Cache 统计）此前调用了
LanceDB 表对象上不存在的 ``query()`` / ``to_list()``，异常被 ``except`` 吞掉，
表现为"不报错但永远无效"。这里针对真实 LanceDB 表做端到端断言，防止回退。
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.config.settings import get_settings
from symbio.core.semantic_cache import (
    SemanticCacheConfig,
    SemanticCacheEngine,
    _escape_sql_literal,
)


def _skip_if_real_embedding():
    if get_settings().model.openai_api_key:
        pytest.skip("环境配置了真实 embedding key")


async def _make_engine(tmp_path, **overrides):
    config = SemanticCacheConfig(
        lancedb_path=str(tmp_path),
        local_embedding_fallback=True,
        similarity_threshold=0.99,
        **overrides,
    )
    engine = SemanticCacheEngine(config)
    await engine.initialize()
    return engine


@pytest.mark.asyncio
async def test_enforce_max_entries_actually_evicts(tmp_path):
    """max_entries 必须真正生效，否则缓存表无上限增长。"""
    _skip_if_real_embedding()
    eng = await _make_engine(tmp_path, max_entries=3)
    try:
        for i in range(8):
            await eng.put(f"问题编号 {i}", f"回答 {i}", model="m")

        assert await eng.get_entry_count() == 3
    finally:
        await eng.close()


@pytest.mark.asyncio
async def test_eviction_keeps_recently_hit_entry(tmp_path):
    """LRU 语义：被命中过的条目应比从未命中的条目更晚淘汰。"""
    _skip_if_real_embedding()
    eng = await _make_engine(tmp_path, max_entries=10)
    try:
        await eng.put("保留这一条", "KEEP", model="m")
        # 命中一次，写入 last_hit_at
        assert await eng.get("保留这一条", model="m") is not None

        eng._config.max_entries = 1
        for i in range(3):
            await eng.put(f"填充条目 {i}", f"填充 {i}", model="m")

        assert await eng.get_entry_count() == 1
        remaining = await eng.get_all_entries()
        assert [e.response_text for e in remaining] == ["KEEP"]
    finally:
        await eng.close()


@pytest.mark.asyncio
async def test_hit_count_persisted_to_table(tmp_path):
    """hit_count / last_hit_at 必须落盘，否则 LRU 没有可排序依据。"""
    _skip_if_real_embedding()
    eng = await _make_engine(tmp_path)
    try:
        await eng.put("统计命中次数", "R", model="m")
        for _ in range(3):
            assert await eng.get("统计命中次数", model="m") is not None

        entries = await eng.get_all_entries()
        assert len(entries) == 1
        assert entries[0].hit_count == 3
        assert entries[0].last_hit_at is not None
    finally:
        await eng.close()


@pytest.mark.asyncio
async def test_get_all_entries_returns_rows_and_respects_limit(tmp_path):
    _skip_if_real_embedding()
    eng = await _make_engine(tmp_path)
    try:
        for i in range(5):
            await eng.put(f"枚举条目 {i}", f"回答 {i}", model="m")

        assert len(await eng.get_all_entries()) == 5
        assert len(await eng.get_all_entries(limit=2)) == 2
    finally:
        await eng.close()


@pytest.mark.asyncio
async def test_prompt_cache_stats_and_entries(tmp_path):
    _skip_if_real_embedding()
    eng = await _make_engine(tmp_path)
    try:
        for i in range(2):
            await eng.put(f"共享前缀 {i}", f"回答 {i}", model="m", prompt_prefix_hash="shared")
        await eng.put("独立前缀", "回答", model="m", prompt_prefix_hash="lonely")
        await eng.put("没有前缀", "回答", model="m")

        stats = await eng.get_prompt_cache_stats()
        assert stats["total_aligned_entries"] == 3
        assert stats["unique_prefixes"] == 2
        assert stats["reusable_prefixes"] == 1
        assert stats["prefix_distribution"]["shared"] == 2

        shared = await eng.get_prompt_cache_entries("shared")
        assert len(shared) == 2
        assert await eng.get_prompt_cache_entries("does-not-exist") == []
    finally:
        await eng.close()


@pytest.mark.asyncio
async def test_invalidate_by_version_and_context(tmp_path):
    _skip_if_real_embedding()
    eng = await _make_engine(tmp_path)
    try:
        await eng.put("旧版本条目", "A", model="m", version="1.0.0", context_hash="ctx-1")
        await eng.put("新版本条目", "B", model="m", version="2.0.0", context_hash="ctx-2")

        assert await eng.invalidate_by_version("1.0.0") == 1
        assert await eng.invalidate_by_context("ctx-2") == 1
        assert await eng.get_entry_count() == 0
    finally:
        await eng.close()


@pytest.mark.asyncio
async def test_quote_in_identifier_does_not_break_where_clause(tmp_path):
    """带单引号的前缀 hash 不能破坏 where 子句语法。"""
    _skip_if_real_embedding()
    eng = await _make_engine(tmp_path)
    try:
        await eng.put("引号前缀", "R", model="m", prompt_prefix_hash="pf'x")
        assert len(await eng.get_prompt_cache_entries("pf'x")) == 1
    finally:
        await eng.close()


def test_escape_sql_literal_doubles_single_quotes():
    assert _escape_sql_literal("a'b") == "a''b"
    assert _escape_sql_literal("plain") == "plain"
