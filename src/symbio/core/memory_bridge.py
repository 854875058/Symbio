"""记忆桥接器 - 连接 Orchestrator 与记忆系统

为 Orchestrator 提供统一的记忆增强接口，内部协调 MemoryManager 和 OntologyEngine。
"""

from __future__ import annotations

import re
from typing import Any, Optional

from symbio.memory.manager import MemoryManager, MemoryType, MemoryPriority
from symbio.memory.ontology import (
    Individual,
    OntologyEngine,
    RelationInstance,
    RelationType,
)
from symbio.utils.logger import get_logger

logger = get_logger("memory_bridge")

# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------

_memory_bridge: Optional[MemoryBridge] = None


async def get_memory_bridge() -> MemoryBridge:
    """获取全局 MemoryBridge 单例（惰性初始化）"""
    global _memory_bridge
    if _memory_bridge is None:
        _memory_bridge = MemoryBridge()
        await _memory_bridge.initialize()
    return _memory_bridge


# ---------------------------------------------------------------------------
# 实体提取正则
# ---------------------------------------------------------------------------

# 匹配大写开头的连续词（如 Python、TensorFlow、OpenAI）
_CAPITALIZED_TERM = re.compile(r"\b([A-Z][a-zA-Z0-9]{1,})\b")

# 匹配文件路径（如 /usr/bin/python、src/main.py、C:\Users\test）
_FILE_PATH = re.compile(r"(?:[A-Za-z]:\\[\w\\./]+|/[\w./]+(?:\.\w+)?)")

# 排除的常见停用词（句首大写、代词等）
_STOP_WORDS: set[str] = {
    "The", "This", "That", "These", "Those", "What", "When", "Where",
    "Which", "Who", "How", "Why", "Can", "Could", "Would", "Should",
    "Will", "Shall", "May", "Might", "Must", "Have", "Has", "Had",
    "Does", "Did", "Are", "Was", "Were", "Been", "Being", "Please",
    "Yes", "No", "Ok", "Well", "Also", "Just", "Now", "Here", "There",
    "Then", "So", "But", "And", "Or", "Not", "For", "If", "In", "On",
    "At", "To", "Of", "By", "As", "It", "Its", "I", "My", "Me", "We",
    "Our", "You", "Your", "He", "She", "His", "Her", "They", "Their",
}


# ---------------------------------------------------------------------------
# MemoryBridge
# ---------------------------------------------------------------------------


class MemoryBridge:
    """桥接 Orchestrator 与记忆系统

    核心职责：
    1. 初始化并管理 MemoryManager 和 OntologyEngine 的生命周期
    2. 为 Orchestrator 提供记忆增强的上下文检索
    3. 将对话和执行结果自动存入记忆
    4. 从对话中提取实体/关系填充本体图谱
    """

    def __init__(self) -> None:
        self.memory_manager: Optional[MemoryManager] = None
        self.ontology: Optional[OntologyEngine] = None
        self._initialized: bool = False
        self._related_to_relation_id: str = ""  # RELATED_TO 关系定义 ID

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """初始化记忆管理器和本体引擎"""
        if self._initialized:
            return

        try:
            self.memory_manager = MemoryManager()
            await self.memory_manager.initialize()
            logger.info("MemoryManager 初始化完成")
        except Exception as e:
            logger.warning(f"MemoryManager 初始化失败（将以无记忆模式运行）: {e}")
            self.memory_manager = None

        try:
            self.ontology = OntologyEngine()
            # 预注册 RELATED_TO 关系定义，用于实体间的通用关联
            from symbio.memory.ontology import RelationDefinition

            rel_def = RelationDefinition(
                name="RELATED_TO",
                relation_type=RelationType.RELATED_TO,
            )
            self._related_to_relation_id = self.ontology.add_relation_definition(rel_def)
            logger.info("OntologyEngine 初始化完成")
        except Exception as e:
            logger.warning(f"OntologyEngine 初始化失败（将以无本体模式运行）: {e}")
            self.ontology = None

        self._initialized = True
        logger.info("MemoryBridge 初始化完成")

    async def close(self) -> None:
        """关闭所有连接"""
        if self.memory_manager:
            try:
                await self.memory_manager.close()
            except Exception as e:
                logger.warning(f"MemoryManager 关闭异常: {e}")

        self.memory_manager = None
        self.ontology = None
        self._initialized = False
        logger.info("MemoryBridge 已关闭")

    # ------------------------------------------------------------------
    # 上下文增强
    # ------------------------------------------------------------------

    async def enhance_context(
        self,
        query: str,
        session_id: str = "",
        max_memories: int = 5,
    ) -> str:
        """为任务检索相关记忆，返回格式化的上下文字符串

        Args:
            query: 查询文本
            session_id: 会话 ID（可选，用于限定搜索范围）
            max_memories: 最大记忆条数

        Returns:
            格式化的上下文字符串；无结果时返回空字符串
        """
        sections: list[str] = []

        # ---- 检索相关记忆 ----
        if self.memory_manager:
            try:
                search_results = await self.memory_manager.search(
                    query, max_results=max_memories
                )
                if search_results:
                    lines: list[str] = ["=== 相关记忆 ==="]
                    for idx, result in enumerate(search_results, 1):
                        title = result.memory.source or result.memory.memory_type.value
                        content_preview = result.memory.content[:80]
                        if len(result.memory.content) > 80:
                            content_preview += "..."
                        lines.append(
                            f"{idx}. [{title}] (相关度: {result.score:.2f}): "
                            f"{content_preview}"
                        )
                    sections.append("\n".join(lines))
            except Exception as e:
                logger.warning(f"记忆检索失败: {e}")

        # ---- 检索相关概念 ----
        if self.ontology:
            try:
                query_result = self.ontology.query(query)
                if query_result.results:
                    lines = ["=== 相关概念 ==="]
                    for item in query_result.results:
                        item_type = item.get("type", "unknown")
                        name = item.get("name", "")
                        if item_type == "concept":
                            desc = item.get("description", "")
                            lines.append(f"- 概念: {name}" + (f" - {desc}" if desc else ""))
                        elif item_type == "individual":
                            concepts = item.get("concepts", [])
                            props = item.get("properties", {})
                            parts = [f"- 个体: {name}"]
                            if concepts:
                                parts.append(f" (类型: {', '.join(concepts)})")
                            if props:
                                prop_str = ", ".join(
                                    f"{k}={v}" for k, v in props.items()
                                )
                                parts.append(f" | 属性: {prop_str}")
                            lines.append("".join(parts))
                        elif item_type == "relation":
                            rel_name = item.get("relation", "")
                            source = item.get("source", "")
                            target = item.get("target", "")
                            lines.append(f"- 关系: {source} --[{rel_name}]--> {target}")
                        elif item_type in ("inferred_concept", "inferred_properties"):
                            lines.append(f"- 推理: {name} -> {item}")
                    sections.append("\n".join(lines))
            except Exception as e:
                logger.warning(f"本体查询失败: {e}")

        if not sections:
            return ""

        return "\n\n".join(sections)

    # ------------------------------------------------------------------
    # 存储接口
    # ------------------------------------------------------------------

    async def store_conversation(
        self,
        session_id: str,
        role: str,
        content: str,
    ) -> None:
        """将对话轮次存入记忆

        Args:
            session_id: 会话 ID
            role: 角色 (user / assistant / system)
            content: 对话内容
        """
        if not self.memory_manager:
            logger.debug("MemoryManager 不可用，跳过对话存储")
            return

        try:
            await self.memory_manager.add_conversation_turn(
                role=role,
                content=content,
                session_id=session_id,
            )
            logger.debug(f"对话轮次已存储: session={session_id}, role={role}")
        except Exception as e:
            logger.warning(f"对话轮次存储失败: {e}")
            return

        # 从内容中提取实体并存入本体
        try:
            await self.extract_and_store_entities(content, source="conversation")
        except Exception as e:
            logger.debug(f"对话实体提取跳过: {e}")

    async def store_execution_result(
        self,
        task_id: str,
        result_content: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """将任务执行结果存入长期记忆

        Args:
            task_id: 任务 ID
            result_content: 执行结果内容
            metadata: 额外元数据；可包含 ``success`` 键来决定重要性
        """
        if not self.memory_manager:
            logger.debug("MemoryManager 不可用，跳过执行结果存储")
            return

        meta = metadata or {}
        success = meta.get("success", True)
        importance = 0.6 if success else 0.8  # 失败更重要，值得记住

        tags = ["execution_result", f"task:{task_id}"]
        if not success:
            tags.append("failure")

        try:
            await self.memory_manager.add_memory(
                content=result_content,
                memory_type=MemoryType.LONG_TERM,
                priority=MemoryPriority.HIGH if not success else MemoryPriority.NORMAL,
                source=f"task:{task_id}",
                tags=tags,
                importance=importance,
                metadata={"task_id": task_id, **meta},
            )
            logger.info(
                f"执行结果已存储: task={task_id}, importance={importance}, "
                f"success={success}"
            )
        except Exception as e:
            logger.warning(f"执行结果存储失败: {e}")

    # ------------------------------------------------------------------
    # 实体提取
    # ------------------------------------------------------------------

    async def extract_and_store_entities(
        self,
        text: str,
        source: str = "conversation",
    ) -> list[str]:
        """从文本中提取实体并存入本体图谱

        提取规则：
        - 大写开头的技术术语（排除常见停用词）
        - 文件路径

        Args:
            text: 待提取文本
            source: 来源标识

        Returns:
            提取到的实体名称列表
        """
        entities = self._extract_entities(text)
        if not entities:
            return []

        stored: list[str] = []
        individual_ids: list[str] = []

        if not self.ontology:
            logger.debug("OntologyEngine 不可用，跳过实体存储")
            return entities

        for entity_name in entities:
            try:
                # 检查是否已存在
                existing_id = self.ontology._name_to_individual.get(entity_name.lower())
                if existing_id:
                    stored.append(entity_name)
                    individual_ids.append(existing_id)
                    continue

                individual = Individual(
                    name=entity_name,
                    properties={"source": source},
                    metadata={"extracted_from": source},
                )
                ind_id = self.ontology.add_individual(individual)
                stored.append(entity_name)
                individual_ids.append(ind_id)
                logger.debug(f"实体已存入本体: {entity_name} (id={ind_id})")
            except Exception as e:
                logger.debug(f"实体存储失败 [{entity_name}]: {e}")

        # 如果提取到多个实体，两两之间添加 RELATED_TO 关系
        if len(individual_ids) >= 2 and self._related_to_relation_id:
            for i in range(len(individual_ids)):
                for j in range(i + 1, len(individual_ids)):
                    try:
                        rel = RelationInstance(
                            relation_id=self._related_to_relation_id,
                            source_id=individual_ids[i],
                            target_id=individual_ids[j],
                            weight=0.5,
                            metadata={"source": source},
                        )
                        self.ontology.add_relation_instance(rel)
                    except Exception as e:
                        logger.debug(f"关系实例创建失败: {e}")

        return stored

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        """获取记忆系统统计信息

        Returns:
            包含 memory 和 ontology 统计的字典
        """
        stats: dict[str, Any] = {"initialized": self._initialized}

        if self.memory_manager:
            try:
                mem_stats = self.memory_manager.get_stats()
                stats["memory"] = mem_stats.model_dump()
            except Exception as e:
                logger.warning(f"获取 MemoryManager 统计失败: {e}")
                stats["memory"] = {"error": str(e)}
        else:
            stats["memory"] = None

        if self.ontology:
            try:
                stats["ontology"] = self.ontology.get_statistics()
            except Exception as e:
                logger.warning(f"获取 OntologyEngine 统计失败: {e}")
                stats["ontology"] = {"error": str(e)}
        else:
            stats["ontology"] = None

        return stats

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_entities(text: str) -> list[str]:
        """从文本中提取实体名称

        提取大写开头的术语和文件路径，排除停用词。
        """
        entities: list[str] = []
        seen_lower: set[str] = set()

        # 1. 文件路径
        for match in _FILE_PATH.finditer(text):
            path = match.group(0)
            if path.lower() not in seen_lower:
                seen_lower.add(path.lower())
                entities.append(path)

        # 2. 大写开头的技术术语
        for match in _CAPITALIZED_TERM.finditer(text):
            term = match.group(1)
            if term in _STOP_WORDS:
                continue
            if len(term) < 2:
                continue
            if term.lower() not in seen_lower:
                seen_lower.add(term.lower())
                entities.append(term)

        return entities
