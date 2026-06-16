"""Pytest 全局配置：隔离持久化存储，保证测试确定性。

语义缓存（SemanticCacheEngine）默认带本地 embedding 降级后，会真正向 LanceDB
写入并命中。若直接用项目的 ./data/lancedb，缓存条目会跨测试运行残留，导致
依赖"每次对话都是全新 LLM 调用"的集成测试（如 token_usage 断言）出现偶发命中。

这里在任何 symbio 模块导入前，把 LanceDB 路径指向一个每次会话全新的临时目录，
从而让缓存对测试隔离、每次会话干净。
"""

import os
import tempfile

# 必须在导入 symbio 之前设置（get_settings 运行时才读取，这里足够早）
_SESSION_DATA_DIR = tempfile.mkdtemp(prefix="symbio_test_")
os.environ["SYMBIO_MEMORY_LANCEDB_PATH"] = os.path.join(_SESSION_DATA_DIR, "lancedb")
