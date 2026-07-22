"""Phase 3 记忆系统集成测试

覆盖模块：
1. EntityExtractor - 实体提取器（文件路径、URL、技术术语、类名等）
2. RelationExtractor - 关系提取器（uses、depends_on、共现关系）
3. AutoPopulator - 自动填充器（从文本/对话提取并填充本体）
4. MemoryBridge - 记忆桥接器（Orchestrator 与记忆系统的连接）
5. Orchestrator + Memory 集成（记忆上下文注入、执行结果存储）

Mock 策略：
- LanceDB 使用纯内存模式（lancedb 未安装时自动回退）
- Anthropic API 使用 unittest.mock.AsyncMock
- 遵循 test_phase2.py 的 pytest 类组织模式
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


# 确保 src 在 Python 路径中
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.memory.auto_populator import (
    AutoPopulator,
    Entity,
    EntityExtractor,
    PopulateResult,
    RelationExtractor,
)
from symbio.memory.manager import (
    MemoryItem,
    MemoryManager,
    MemoryPriority,
    MemoryType,
    SearchResult,
)
from symbio.memory.ontology import (
    OntologyEngine,
)
from symbio.core.memory_bridge import MemoryBridge


# ================================================================
# 共用辅助函数
# ================================================================


def _make_mock_memory_manager():
    """创建一个 mock MemoryManager，避免 LanceDB 依赖。"""
    manager = MagicMock(spec=MemoryManager)
    manager.initialize = AsyncMock()
    manager.close = AsyncMock()
    manager.add_memory = AsyncMock(
        return_value=MemoryItem(
            content="test",
            memory_type=MemoryType.LONG_TERM,
        )
    )
    manager.add_conversation_turn = AsyncMock()
    manager.search = AsyncMock(return_value=[])
    manager.get_stats = MagicMock(
        return_value=MagicMock(
            model_dump=MagicMock(
                return_value={
                    "total_memories": 0,
                    "short_term_count": 0,
                    "long_term_count": 0,
                }
            ),
        )
    )
    return manager


def _make_search_result(content, source="test", score=0.9, memory_type=MemoryType.LONG_TERM):
    """创建一个 SearchResult 用于测试。"""
    memory = MemoryItem(
        content=content,
        source=source,
        memory_type=memory_type,
        importance=0.7,
    )
    return SearchResult(memory=memory, score=score, match_type="semantic")


# ================================================================
# 1. EntityExtractor 测试
# ================================================================


class TestEntityExtractor:
    """实体提取器测试"""

    def test_extract_file_paths(self):
        """从文本中提取文件路径（需要含扩展名）"""
        extractor = EntityExtractor()
        text = "请查看 src/main.py 和 config/settings.yaml 这两个文件"
        entities = extractor.extract(text)

        names = {e.name for e in entities}
        # file_path 模式匹配含扩展名的路径
        assert "src/main.py" in names
        assert "config/settings.yaml" in names

        # 验证实体类型
        for e in entities:
            if e.name == "src/main.py":
                assert e.entity_type == "file_path"

    def test_extract_urls(self):
        """从文本中提取 URL"""
        extractor = EntityExtractor()
        text = "访问 https://example.com/api 和 http://localhost:8080 获取数据"
        entities = extractor.extract(text)

        names = {e.name for e in entities}
        assert "https://example.com/api" in names
        assert "http://localhost:8080" in names

        for e in entities:
            if e.entity_type == "url":
                assert e.confidence >= 0.9

    def test_extract_email_addresses(self):
        """从文本中提取邮箱地址"""
        extractor = EntityExtractor()
        text = "请联系 admin@example.com 或 user@test.org"
        entities = extractor.extract(text)

        names = {e.name for e in entities}
        assert "admin@example.com" in names
        assert "user@test.org" in names

        for e in entities:
            if e.entity_type == "email":
                assert e.confidence >= 0.9

    def test_extract_camelcase_class_names(self):
        """从文本中提取 CamelCase 类名"""
        extractor = EntityExtractor()
        text = "MemoryManager 继承自 BaseManager，使用 OntologyEngine 进行推理"
        entities = extractor.extract(text)

        names = {e.name for e in entities}
        assert "MemoryManager" in names
        assert "BaseManager" in names
        assert "OntologyEngine" in names

        for e in entities:
            if e.name == "MemoryManager":
                assert e.entity_type in ("class_name", "tech_term")

    def test_extract_tech_terms(self):
        """从文本中提取技术术语（Python、Docker 等）"""
        extractor = EntityExtractor()
        text = "使用 Python 和 Docker 部署，数据库选择 PostgreSQL"
        entities = extractor.extract(text)

        names = {e.name for e in entities}
        assert "Python" in names
        assert "Docker" in names
        assert "PostgreSQL" in names

        for e in entities:
            if e.entity_type == "tech_term":
                assert e.confidence >= 0.9

    def test_handle_empty_text(self):
        """空文本应返回空列表"""
        extractor = EntityExtractor()
        assert extractor.extract("") == []
        assert extractor.extract("   ") == []

    def test_stop_word_filtering(self):
        """停用词不应被识别为类名实体"""
        extractor = EntityExtractor()
        # "The", "This", "When" 等都是停用词
        text = "The system When started This module was initialized"
        entities = extractor.extract(text)

        names = {e.name for e in entities}
        stop_words_in_results = names & extractor._STOP_WORDS
        assert len(stop_words_in_results) == 0, f"停用词不应出现在实体中: {stop_words_in_results}"

    def test_entity_deduplication(self):
        """同一实体不应重复出现"""
        extractor = EntityExtractor()
        text = "Python is great. I love Python. Python rocks."
        entities = extractor.extract(text)

        names = [e.name for e in entities]
        # Python 应该只出现一次
        assert names.count("Python") == 1

    def test_entity_context_captured(self):
        """提取的实体应包含上下文信息"""
        extractor = EntityExtractor()
        text = "我们使用 Docker 容器化部署应用"
        entities = extractor.extract(text)

        docker_entity = next((e for e in entities if e.name == "Docker"), None)
        assert docker_entity is not None
        assert len(docker_entity.context) > 0

    def test_version_extraction(self):
        """从文本中提取版本号"""
        extractor = EntityExtractor()
        text = "升级到 Python v3.12.1 和 Node.js 18.17.0"
        entities = extractor.extract(text)

        names = {e.name for e in entities}
        assert "v3.12.1" in names or "3.12.1" in names
        assert "18.17.0" in names


# ================================================================
# 2. RelationExtractor 测试
# ================================================================


class TestRelationExtractor:
    """关系提取器测试"""

    def test_extract_uses_relation(self):
        """提取 'uses' 关系（A uses B）"""
        extractor = RelationExtractor()
        text = "Python uses NumPy for computation"
        entities = [
            Entity(name="Python", entity_type="tech_term"),
            Entity(name="NumPy", entity_type="tech_term"),
        ]
        relations = extractor.extract(text, entities)

        uses_rels = [r for r in relations if r.relation_type == "uses"]
        assert len(uses_rels) >= 1
        assert uses_rels[0].source == "Python"
        assert uses_rels[0].target == "NumPy"

    def test_extract_depends_on_relation(self):
        """提取 'depends_on' 关系（A depends on B）"""
        extractor = RelationExtractor()
        text = "ServiceA depends on ServiceB for authentication"
        entities = [
            Entity(name="ServiceA", entity_type="class_name"),
            Entity(name="ServiceB", entity_type="class_name"),
        ]
        relations = extractor.extract(text, entities)

        depends_rels = [r for r in relations if r.relation_type == "depends_on"]
        assert len(depends_rels) >= 1
        assert depends_rels[0].source == "ServiceA"
        assert depends_rels[0].target == "ServiceB"

    def test_co_occurrence_detection(self):
        """同一句子中出现的实体应产生共现关系"""
        extractor = RelationExtractor()
        text = "Docker and Kubernetes are used together"
        entities = [
            Entity(name="Docker", entity_type="tech_term"),
            Entity(name="Kubernetes", entity_type="tech_term"),
        ]
        relations = extractor.extract(text, entities)

        # 应该有 related_to 或 uses 关系
        assert len(relations) >= 1
        related = [r for r in relations if r.relation_type in ("related_to", "uses")]
        assert len(related) >= 1

    def test_relation_deduplication(self):
        """相同关系不应重复提取"""
        extractor = RelationExtractor()
        # 多次提到相同关系
        text = "Python uses NumPy. Python uses NumPy for math."
        entities = [
            Entity(name="Python", entity_type="tech_term"),
            Entity(name="NumPy", entity_type="tech_term"),
        ]
        relations = extractor.extract(text, entities)

        uses_rels = [r for r in relations if r.relation_type == "uses"]
        # 应去重为 1 个
        assert len(uses_rels) == 1

    def test_relation_confidence(self):
        """不同关系模式应有不同置信度"""
        extractor = RelationExtractor()
        text = "AppA depends on AppB"
        entities = [
            Entity(name="AppA", entity_type="class_name"),
            Entity(name="AppB", entity_type="class_name"),
        ]
        relations = extractor.extract(text, entities)

        depends_rels = [r for r in relations if r.relation_type == "depends_on"]
        if depends_rels:
            assert depends_rels[0].confidence > 0.5

    def test_empty_entities_returns_empty(self):
        """无实体时不应提取到关系"""
        extractor = RelationExtractor()
        text = "This is a simple sentence with no entities."
        relations = extractor.extract(text, [])
        assert len(relations) == 0


# ================================================================
# 3. AutoPopulator 测试
# ================================================================


class TestAutoPopulator:
    """自动填充器测试"""

    def _make_populator(self):
        """创建带 OntologyEngine 的 AutoPopulator"""
        ontology = OntologyEngine()
        return AutoPopulator(ontology), ontology

    async def test_populate_from_single_text(self):
        """从单段文本中提取实体并存入本体"""
        populator, ontology = self._make_populator()

        text = "我们使用 Python 和 Docker 部署服务"
        result = await populator.populate_from_text(text)

        assert result.entities_found > 0
        assert result.entities_stored > 0
        # 验证本体中有个体
        assert len(ontology._individuals) > 0

    async def test_populate_from_conversation_turns(self):
        """从对话轮次列表中批量提取"""
        populator, ontology = self._make_populator()

        turns = [
            {"role": "user", "content": "我需要使用 PostgreSQL 数据库"},
            {"role": "assistant", "content": "好的，PostgreSQL 是一个很好的选择"},
            {"role": "user", "content": "还需要 Redis 做缓存"},
        ]
        result = await populator.populate_from_conversation(turns)

        assert result.entities_found > 0
        assert result.entities_stored > 0
        # 应从多轮对话中累积提取
        individual_names = {ind.name.lower() for ind in ontology._individuals.values()}
        assert "postgresql" in individual_names
        assert "redis" in individual_names

    async def test_deduplication_same_entity_twice(self):
        """同一实体出现两次应去重，不应创建两个个体"""
        populator, ontology = self._make_populator()

        await populator.populate_from_text("Python is great")
        count_after_first = len(ontology._individuals)

        await populator.populate_from_text("I love Python")
        count_after_second = len(ontology._individuals)

        # Python 个体数不应增加
        assert count_after_second == count_after_first

    async def test_concept_hierarchy_creation(self):
        """自动填充器应创建概念层级结构"""
        populator, ontology = self._make_populator()

        await populator.populate_from_text("使用 Docker 和 PostgreSQL 部署")

        # 应自动创建 Technology 概念（tech_term 映射）
        concept_names = {c.name for c in ontology._concepts.values()}
        assert "Technology" in concept_names or "Entity" in concept_names

        # 检查概念层级：Technology 的父概念应为 Entity
        tech_concept = None
        for c in ontology._concepts.values():
            if c.name == "Technology":
                tech_concept = c
                break
        if tech_concept:
            assert len(tech_concept.parent_concepts) > 0
            parent_names = {
                ontology._concepts[pid].name
                for pid in tech_concept.parent_concepts
                if pid in ontology._concepts
            }
            assert "Entity" in parent_names

    async def test_populate_empty_text(self):
        """空文本应返回零计数结果"""
        populator, _ = self._make_populator()

        result = await populator.populate_from_text("")
        assert result.entities_found == 0
        assert result.entities_stored == 0
        assert result.relations_found == 0

    async def test_populate_result_model(self):
        """填充结果应为 PopulateResult 类型"""
        populator, _ = self._make_populator()

        result = await populator.populate_from_text("Python uses Docker")
        assert isinstance(result, PopulateResult)
        assert hasattr(result, "entities_found")
        assert hasattr(result, "entities_stored")
        assert hasattr(result, "relations_found")
        assert hasattr(result, "relations_stored")
        assert hasattr(result, "new_individuals")


# ================================================================
# 4. MemoryBridge 测试
# ================================================================


class TestMemoryBridge:
    """记忆桥接器测试"""

    async def test_initialize_and_close(self):
        """初始化和关闭应正常完成，无异常"""
        bridge = MemoryBridge()

        # Mock MemoryManager 和 OntologyEngine 的初始化
        with (
            patch("symbio.core.memory_bridge.MemoryManager") as MockMM,
            patch("symbio.core.memory_bridge.OntologyEngine") as MockOE,
        ):
            mock_mm = _make_mock_memory_manager()
            MockMM.return_value = mock_mm

            mock_oe = OntologyEngine()
            MockOE.return_value = mock_oe

            await bridge.initialize()

            assert bridge._initialized is True
            assert bridge.memory_manager is not None
            assert bridge.ontology is not None

            await bridge.close()

            assert bridge._initialized is False
            assert bridge.memory_manager is None
            assert bridge.ontology is None

    async def test_store_conversation(self):
        """存储对话轮次应调用 MemoryManager"""
        bridge = MemoryBridge()

        with (
            patch("symbio.core.memory_bridge.MemoryManager") as MockMM,
            patch("symbio.core.memory_bridge.OntologyEngine") as MockOE,
        ):
            mock_mm = _make_mock_memory_manager()
            MockMM.return_value = mock_mm
            MockOE.return_value = OntologyEngine()

            await bridge.initialize()

            await bridge.store_conversation(
                session_id="test-session",
                role="user",
                content="你好，我需要帮助",
            )

            mock_mm.add_conversation_turn.assert_called_once()
            call_kwargs = mock_mm.add_conversation_turn.call_args
            assert call_kwargs.kwargs["role"] == "user"
            assert call_kwargs.kwargs["content"] == "你好，我需要帮助"
            assert call_kwargs.kwargs["session_id"] == "test-session"

    async def test_store_execution_result_success_importance(self):
        """成功执行结果应使用 importance=0.6"""
        bridge = MemoryBridge()

        with (
            patch("symbio.core.memory_bridge.MemoryManager") as MockMM,
            patch("symbio.core.memory_bridge.OntologyEngine") as MockOE,
        ):
            mock_mm = _make_mock_memory_manager()
            MockMM.return_value = mock_mm
            MockOE.return_value = OntologyEngine()

            await bridge.initialize()

            await bridge.store_execution_result(
                task_id="task-001",
                result_content="任务成功完成",
                metadata={"success": True},
            )

            mock_mm.add_memory.assert_called_once()
            call_kwargs = mock_mm.add_memory.call_args.kwargs
            assert call_kwargs["importance"] == 0.6
            assert call_kwargs["memory_type"] == MemoryType.LONG_TERM
            assert call_kwargs["priority"] == MemoryPriority.NORMAL

    async def test_store_execution_result_failure_importance(self):
        """失败执行结果应使用 importance=0.8 和 HIGH 优先级"""
        bridge = MemoryBridge()

        with (
            patch("symbio.core.memory_bridge.MemoryManager") as MockMM,
            patch("symbio.core.memory_bridge.OntologyEngine") as MockOE,
        ):
            mock_mm = _make_mock_memory_manager()
            MockMM.return_value = mock_mm
            MockOE.return_value = OntologyEngine()

            await bridge.initialize()

            await bridge.store_execution_result(
                task_id="task-002",
                result_content="任务执行失败",
                metadata={"success": False},
            )

            mock_mm.add_memory.assert_called_once()
            call_kwargs = mock_mm.add_memory.call_args.kwargs
            assert call_kwargs["importance"] == 0.8
            assert call_kwargs["priority"] == MemoryPriority.HIGH
            assert "failure" in call_kwargs["tags"]

    async def test_extract_and_store_entities(self):
        """从文本中提取实体并存入本体"""
        bridge = MemoryBridge()

        with (
            patch("symbio.core.memory_bridge.MemoryManager") as MockMM,
            patch("symbio.core.memory_bridge.OntologyEngine") as MockOE,
        ):
            MockMM.return_value = _make_mock_memory_manager()
            MockOE.return_value = OntologyEngine()

            await bridge.initialize()

            entities = await bridge.extract_and_store_entities(
                "使用 Python 和 Docker 部署到 /app/main.py",
            )

            assert len(entities) > 0
            # 本体中应有个体
            assert len(bridge.ontology._individuals) > 0

    async def test_get_stats(self):
        """get_stats 应返回包含 memory 和 ontology 的统计"""
        bridge = MemoryBridge()

        with (
            patch("symbio.core.memory_bridge.MemoryManager") as MockMM,
            patch("symbio.core.memory_bridge.OntologyEngine") as MockOE,
        ):
            MockMM.return_value = _make_mock_memory_manager()
            MockOE.return_value = OntologyEngine()

            await bridge.initialize()

            stats = bridge.get_stats()

            assert stats["initialized"] is True
            assert "memory" in stats
            assert "ontology" in stats
            # ontology 统计应包含 tbox/abox
            assert "tbox" in stats["ontology"]
            assert "abox" in stats["ontology"]

    async def test_enhance_context_empty_memory(self):
        """无记忆时 enhance_context 应返回空字符串"""
        bridge = MemoryBridge()

        with (
            patch("symbio.core.memory_bridge.MemoryManager") as MockMM,
            patch("symbio.core.memory_bridge.OntologyEngine") as MockOE,
        ):
            mock_mm = _make_mock_memory_manager()
            mock_mm.search = AsyncMock(return_value=[])
            MockMM.return_value = mock_mm

            mock_oe = OntologyEngine()
            MockOE.return_value = mock_oe

            await bridge.initialize()

            result = await bridge.enhance_context(query="测试查询")
            assert result == ""

    async def test_enhance_context_with_mocked_memories(self):
        """有记忆时 enhance_context 应返回格式化上下文"""
        bridge = MemoryBridge()

        with (
            patch("symbio.core.memory_bridge.MemoryManager") as MockMM,
            patch("symbio.core.memory_bridge.OntologyEngine") as MockOE,
        ):
            mock_mm = _make_mock_memory_manager()
            search_results = [
                _make_search_result("之前讨论过 Python 优化", source="对话"),
            ]
            mock_mm.search = AsyncMock(return_value=search_results)
            MockMM.return_value = mock_mm

            mock_oe = OntologyEngine()
            MockOE.return_value = mock_oe

            await bridge.initialize()

            result = await bridge.enhance_context(query="Python 优化")

            assert "相关记忆" in result
            assert "Python 优化" in result


# ================================================================
# 5. Orchestrator + Memory 集成测试
# ================================================================


class TestOrchestratorMemoryIntegration:
    """Orchestrator 与记忆系统集成测试"""

    async def test_orchestrator_stores_execution_result(self):
        """Orchestrator 执行任务后应将结果存入记忆"""
        from symbio.core.orchestrator import Orchestrator
        from symbio.utils.types import (
            AgentState,
            Message,
            MessageSource,
            Result,
        )

        orchestrator = Orchestrator()

        # Mock 设置
        mock_settings = MagicMock()
        mock_settings.model = MagicMock()
        mock_settings.model.anthropic_api_key = ""
        mock_settings.model.anthropic_base_url = "https://api.anthropic.com"
        mock_settings.model.model_low = "claude-3-5-haiku-20241022"
        mock_settings.model.model_medium = "claude-sonnet-4-20250514"
        mock_settings.model.model_high = "claude-opus-4-20250514"
        mock_settings.model.openai_api_key = ""
        mock_settings.model.openai_base_url = "https://api.openai.com/v1"

        mock_agent = MagicMock()
        mock_agent.name = "general"
        mock_agent.state = AgentState.IDLE
        mock_agent.execute = AsyncMock(
            return_value=Result(task_id="test", success=True, content="执行完成")
        )
        mock_agent.can_handle = MagicMock(return_value=True)

        # Mock MemoryBridge
        mock_bridge = MagicMock(spec=MemoryBridge)
        mock_bridge._initialized = False
        mock_bridge.initialize = AsyncMock()
        mock_bridge.enhance_context = AsyncMock(return_value="")
        mock_bridge.store_execution_result = AsyncMock()
        mock_bridge.store_conversation = AsyncMock()
        mock_bridge.get_stats = MagicMock(return_value={})

        orchestrator.memory_bridge = mock_bridge

        with (
            patch("symbio.core.orchestrator.get_settings", return_value=mock_settings),
            patch.object(orchestrator.registry, "find_best", return_value=mock_agent),
            patch.object(orchestrator.registry, "get", return_value=mock_agent),
        ):
            message = Message(
                source=MessageSource.CLI,
                user_id="test-user",
                content="测试消息",
                session_id="test-session",
            )
            await orchestrator.process(message)

        # 验证 store_execution_result 被调用
        mock_bridge.store_execution_result.assert_called_once()
        call_kwargs = mock_bridge.store_execution_result.call_args.kwargs
        assert call_kwargs["result_content"] == "执行完成"
        assert call_kwargs["metadata"]["success"] is True

    async def test_memory_context_injected_into_task_metadata(self):
        """记忆上下文应被注入到 task.metadata 中"""
        from symbio.core.orchestrator import Orchestrator
        from symbio.utils.types import (
            AgentState,
            Message,
            MessageSource,
            Result,
        )

        orchestrator = Orchestrator()

        mock_settings = MagicMock()
        mock_settings.model = MagicMock()
        mock_settings.model.anthropic_api_key = ""
        mock_settings.model.anthropic_base_url = "https://api.anthropic.com"
        mock_settings.model.model_low = "claude-3-5-haiku-20241022"
        mock_settings.model.model_medium = "claude-sonnet-4-20250514"
        mock_settings.model.model_high = "claude-opus-4-20250514"
        mock_settings.model.openai_api_key = ""
        mock_settings.model.openai_base_url = "https://api.openai.com/v1"

        mock_agent = MagicMock()
        mock_agent.name = "general"
        mock_agent.state = AgentState.IDLE
        mock_agent.execute = AsyncMock(
            return_value=Result(task_id="test", success=True, content="完成")
        )
        mock_agent.can_handle = MagicMock(return_value=True)

        # Mock MemoryBridge 返回记忆上下文
        mock_bridge = MagicMock(spec=MemoryBridge)
        mock_bridge._initialized = True
        mock_bridge.initialize = AsyncMock()
        mock_bridge.enhance_context = AsyncMock(
            return_value="=== 相关记忆 ===\n1. 之前讨论过 Python 优化"
        )
        mock_bridge.store_execution_result = AsyncMock()
        mock_bridge.store_conversation = AsyncMock()
        mock_bridge.get_stats = MagicMock(return_value={})

        orchestrator.memory_bridge = mock_bridge

        captured_task = None
        original_execute = mock_agent.execute

        async def capturing_execute(task):
            nonlocal captured_task
            captured_task = task
            return await original_execute(task)

        mock_agent.execute = capturing_execute

        with (
            patch("symbio.core.orchestrator.get_settings", return_value=mock_settings),
            patch.object(orchestrator.registry, "find_best", return_value=mock_agent),
            patch.object(orchestrator.registry, "get", return_value=mock_agent),
        ):
            message = Message(
                source=MessageSource.CLI,
                user_id="test-user",
                content="Python 优化",
                session_id="test-session",
            )
            await orchestrator.process(message)

        # 验证任务 metadata 包含记忆上下文
        assert captured_task is not None
        assert "memory_context" in captured_task.metadata
        assert "Python 优化" in captured_task.metadata["memory_context"]

    async def test_general_agent_includes_memory_context_in_prompt(self):
        """GeneralAgent 应将记忆上下文注入 LLM 提示"""
        from symbio.agents.builtin.general_agent import GeneralAgent
        from symbio.utils.types import Intent, Task

        # 创建带记忆上下文的 task
        task = Task(
            intent=Intent(raw_text="测试问题", action="chat"),
        )
        task.metadata["memory_context"] = "=== 相关记忆 ===\n1. 用户喜欢 Python"

        agent = GeneralAgent()
        mock_settings = MagicMock()
        mock_settings.model = MagicMock()
        mock_settings.model.anthropic_api_key = "test-key"
        mock_settings.model.anthropic_base_url = "https://api.anthropic.com"
        mock_settings.model.model_low = "claude-3-5-haiku-20241022"
        mock_settings.model.model_medium = "claude-sonnet-4-20250514"

        # Mock anthropic SDK，捕获发送的消息
        captured_messages = None

        mock_text_block = MagicMock()
        mock_text_block.text = "回复内容"

        mock_response = MagicMock()
        mock_response.content = [mock_text_block]
        mock_response.usage = MagicMock(input_tokens=10, output_tokens=5)
        mock_response.model = "claude-sonnet-4-20250514"

        async def capture_create(**kwargs):
            nonlocal captured_messages
            captured_messages = kwargs.get("messages", [])
            return mock_response

        mock_messages_create = AsyncMock(side_effect=capture_create)

        mock_anthropic_client = MagicMock()
        mock_anthropic_client.messages.create = mock_messages_create

        with (
            patch("symbio.agents.builtin.general_agent.get_settings", return_value=mock_settings),
            patch("anthropic.AsyncAnthropic", return_value=mock_anthropic_client),
        ):
            await agent.execute(task)

        # 验证发送给 LLM 的消息包含记忆上下文
        assert captured_messages is not None
        user_message = captured_messages[0]["content"]
        assert "相关背景知识" in user_message
        assert "Python" in user_message
        assert "用户问题" in user_message

    async def test_general_agent_includes_workflow_guidance_in_prompt(self):
        from symbio.agents.builtin.general_agent import GeneralAgent
        from symbio.utils.types import Intent, Task

        task = Task(intent=Intent(raw_text="Implement a bug fix", action="write_code"))
        task.metadata["workflow_guidance"] = (
            "Workflow policy:\n- Use TDD.\n- Verify before completion."
        )

        agent = GeneralAgent()
        mock_settings = MagicMock()
        mock_settings.model = MagicMock()
        mock_settings.model.anthropic_api_key = "test-key"
        mock_settings.model.anthropic_base_url = "https://api.anthropic.com"
        mock_settings.model.model_medium = "claude-sonnet-4-20250514"

        captured_messages = None
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="done")]
        mock_response.usage = MagicMock(input_tokens=10, output_tokens=5)
        mock_response.model = "claude-sonnet-4-20250514"

        async def capture_create(**kwargs):
            nonlocal captured_messages
            captured_messages = kwargs.get("messages", [])
            return mock_response

        mock_anthropic_client = MagicMock()
        mock_anthropic_client.messages.create = AsyncMock(side_effect=capture_create)

        with (
            patch("symbio.agents.builtin.general_agent.get_settings", return_value=mock_settings),
            patch("anthropic.AsyncAnthropic", return_value=mock_anthropic_client),
        ):
            await agent.execute(task)

        assert captured_messages is not None
        user_message = captured_messages[0]["content"]
        assert "Workflow policy" in user_message
        assert "Use TDD" in user_message
        assert "Implement a bug fix" in user_message

    async def test_orchestrator_get_memory_stats(self):
        """Orchestrator.get_memory_stats 应返回记忆统计"""
        from symbio.core.orchestrator import Orchestrator

        orchestrator = Orchestrator()

        mock_bridge = MagicMock(spec=MemoryBridge)
        mock_bridge.get_stats = MagicMock(
            return_value={
                "initialized": True,
                "memory": {"total_memories": 5},
                "ontology": {"tbox": {"concepts": 3}},
            }
        )
        orchestrator.memory_bridge = mock_bridge

        stats = orchestrator.get_memory_stats()

        assert stats["initialized"] is True
        assert stats["memory"]["total_memories"] == 5
        mock_bridge.get_stats.assert_called_once()

    async def test_orchestrator_memory_failure_does_not_block(self):
        """记忆系统失败不应阻断主流程"""
        from symbio.core.orchestrator import Orchestrator
        from symbio.utils.types import (
            AgentState,
            Message,
            MessageSource,
            Result,
        )

        orchestrator = Orchestrator()

        mock_settings = MagicMock()
        mock_settings.model = MagicMock()
        mock_settings.model.anthropic_api_key = ""
        mock_settings.model.anthropic_base_url = "https://api.anthropic.com"
        mock_settings.model.model_low = "claude-3-5-haiku-20241022"
        mock_settings.model.model_medium = "claude-sonnet-4-20250514"
        mock_settings.model.model_high = "claude-opus-4-20250514"
        mock_settings.model.openai_api_key = ""
        mock_settings.model.openai_base_url = "https://api.openai.com/v1"

        mock_agent = MagicMock()
        mock_agent.name = "general"
        mock_agent.state = AgentState.IDLE
        mock_agent.execute = AsyncMock(
            return_value=Result(task_id="test", success=True, content="成功")
        )
        mock_agent.can_handle = MagicMock(return_value=True)

        # Mock MemoryBridge 抛出异常
        mock_bridge = MagicMock(spec=MemoryBridge)
        mock_bridge._initialized = False
        mock_bridge.initialize = AsyncMock(side_effect=RuntimeError("初始化失败"))
        mock_bridge.enhance_context = AsyncMock(side_effect=RuntimeError("失败"))
        mock_bridge.store_execution_result = AsyncMock(side_effect=RuntimeError("失败"))
        mock_bridge.get_stats = MagicMock(return_value={})

        orchestrator.memory_bridge = mock_bridge

        with (
            patch("symbio.core.orchestrator.get_settings", return_value=mock_settings),
            patch.object(orchestrator.registry, "find_best", return_value=mock_agent),
            patch.object(orchestrator.registry, "get", return_value=mock_agent),
        ):
            message = Message(
                source=MessageSource.CLI,
                user_id="test-user",
                content="测试",
                session_id="test-session",
            )
            # 不应抛出异常
            result = await orchestrator.process(message)

        assert result.success is True
        assert result.content == "成功"
