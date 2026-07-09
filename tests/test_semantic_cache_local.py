"""语义缓存本地 embedding 降级测试 + FixedSizeList schema 回归。"""

from pathlib import Path
import math
import sys

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.core.semantic_cache import (
    SemanticCacheConfig,
    SemanticCacheEngine,
    build_cache_schema,
)


def test_build_schema_vector_is_fixed_size_list():
    import pyarrow as pa
    schema = build_cache_schema(256)
    vec = schema.field("vector")
    # 必须是固定维度 list，否则 LanceDB 向量检索报 "Data type is not a vector"
    assert pa.types.is_fixed_size_list(vec.type)
    assert vec.type.list_size == 256


def test_local_embedding_deterministic_and_normalized():
    eng = SemanticCacheEngine(SemanticCacheConfig(local_embedding_fallback=True))
    # 强制走哈希降级（不依赖 sentence-transformers）
    eng._st_model = False
    a = eng._local_embedding("帮我写个快速排序算法")
    b = eng._local_embedding("帮我写个快速排序算法")
    assert a == b  # 确定性
    assert len(a) == eng._effective_dim
    norm = math.sqrt(sum(x * x for x in a))
    assert norm == pytest.approx(1.0, abs=1e-6)  # L2 归一化


def test_backend_selection_without_key():
    eng = SemanticCacheEngine(SemanticCacheConfig(local_embedding_fallback=True))
    from symbio.config.settings import get_settings
    if get_settings().model.openai_api_key:
        pytest.skip("环境配置了真实 embedding key")
    assert eng._embedding_backend == "local"
    # 表名格式为 <base>_<backend>_<dim>，维度随本地后端（ST 384 / 哈希 256）而定
    assert f"_local_{eng._effective_dim}" in eng._config.table_name
    assert eng._effective_dim in (256, 384)


@pytest.mark.asyncio
async def test_local_cache_hit_and_miss(tmp_path):
    from symbio.config.settings import get_settings
    if get_settings().model.openai_api_key:
        pytest.skip("环境配置了真实 embedding key")
    eng = SemanticCacheEngine(SemanticCacheConfig(
        lancedb_path=str(tmp_path), local_embedding_fallback=True, similarity_threshold=0.9,
    ))
    await eng.initialize()
    try:
        await eng.put("帮我写个快速排序算法", "QUICKSORT", model="m")
        hit = await eng.get("帮我写个快速排序算法", model="m")
        assert hit is not None and hit.response_text == "QUICKSORT"
        miss = await eng.get("完全不相关的问题：今天星期几", model="m")
        assert miss is None
        stats = eng.get_stats()
        assert stats.cache_hits == 1
        assert stats.cache_misses == 1
    finally:
        await eng.close()


@pytest.mark.asyncio
async def test_local_cache_context_isolation(tmp_path):
    from symbio.config.settings import get_settings
    if get_settings().model.openai_api_key:
        pytest.skip("环境配置了真实 embedding key")
    eng = SemanticCacheEngine(SemanticCacheConfig(
        lancedb_path=str(tmp_path), local_embedding_fallback=True, similarity_threshold=0.9,
    ))
    await eng.initialize()
    try:
        await eng.put("同样的问题", "A", model="m", context_hash="ctx-1")
        # 不同上下文指纹不应命中
        miss = await eng.get("同样的问题", model="m", current_context_hash="ctx-2")
        assert miss is None
        # 相同上下文命中
        hit = await eng.get("同样的问题", model="m", current_context_hash="ctx-1")
        assert hit is not None and hit.response_text == "A"
    finally:
        await eng.close()
