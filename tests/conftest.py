"""Pytest 全局配置：隔离持久化存储，保证测试确定性。

语义缓存（SemanticCacheEngine）默认带本地 embedding 降级后，会真正向 LanceDB
写入并命中。若直接用项目的 ./data/lancedb，缓存条目会跨测试运行残留，导致
依赖"每次对话都是全新 LLM 调用"的集成测试（如 token_usage 断言）出现偶发命中。

这里在任何 symbio 模块导入前，把 LanceDB 路径指向一个每次会话全新的临时目录，
从而让缓存对测试隔离、每次会话干净。
"""

import os
import tempfile
import weakref

import pytest

# 必须在导入 symbio 之前设置（get_settings 运行时才读取，这里足够早）
_SESSION_DATA_DIR = tempfile.mkdtemp(prefix="symbio_test_")
os.environ["SYMBIO_MEMORY_LANCEDB_PATH"] = os.path.join(_SESSION_DATA_DIR, "lancedb")
_TRACKED_GATEWAYS = weakref.WeakSet()


@pytest.fixture(autouse=True)
async def _isolate_runtime_state(monkeypatch):
    """隔离全局配置缓存，并关闭测试期间创建的 Orchestrator 资源。"""
    from symbio.config.settings import get_settings
    from symbio.core.chat_pipeline import shutdown_chat_pipeline
    from symbio.core.hitl_gateway import ApprovalGateway
    from symbio.core.orchestrator import Orchestrator
    from symbio.evolution.flywheel import shutdown_flywheel
    from symbio.interfaces.database import close_db

    orchestrators = []
    original_orchestrator_init = Orchestrator.__init__
    original_gateway_init = ApprovalGateway.__init__

    def tracked_orchestrator_init(self, *args, **kwargs):
        original_orchestrator_init(self, *args, **kwargs)
        orchestrators.append(self)

    def tracked_gateway_init(self, *args, **kwargs):
        original_gateway_init(self, *args, **kwargs)
        _TRACKED_GATEWAYS.add(self)

    monkeypatch.setattr(Orchestrator, "__init__", tracked_orchestrator_init)
    monkeypatch.setattr(ApprovalGateway, "__init__", tracked_gateway_init)
    get_settings.cache_clear()
    try:
        yield
    finally:
        for orchestrator in reversed(orchestrators):
            await orchestrator.close()
        for gateway in list(_TRACKED_GATEWAYS):
            await gateway.close()
        await shutdown_chat_pipeline()
        await shutdown_flywheel()
        await close_db()
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 慢测试分层：默认跳过 @pytest.mark.slow（真跑浏览器/加载模型/真训练），
# 加 --run-slow 才执行。让本地日常迭代只跑快测试（约 40s vs 全量 130s）。
# CI 全量校验时传 --run-slow。
# ---------------------------------------------------------------------------


def pytest_addoption(parser):
    parser.addoption(
        "--run-slow",
        action="store_true",
        default=False,
        help="运行标记为 slow 的慢测试（浏览器/模型加载/真训练）",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-slow"):
        return
    skip_slow = pytest.mark.skip(reason="慢测试默认跳过，加 --run-slow 运行")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip_slow)
