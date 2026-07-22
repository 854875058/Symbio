"""自动填充器 - 从文本中提取实体和关系，自动填充本体图谱"""

from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, Field

from symbio.memory.ontology import (
    Concept,
    Individual,
    OntologyEngine,
    RelationDefinition,
    RelationInstance,
    RelationType,
)
from symbio.utils.logger import get_logger

logger = get_logger("auto_populator")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


class Entity(BaseModel):
    """从文本中提取的实体"""

    name: str
    entity_type: str  # "file_path", "url", "tech_term", "class_name", "general", etc.
    confidence: float = 0.8
    context: str = ""  # surrounding text


class Relation(BaseModel):
    """从文本中提取的关系"""

    source: str  # entity name
    target: str  # entity name
    relation_type: str  # "uses", "depends_on", "part_of", "related_to", etc.
    confidence: float = 0.7
    evidence: str = ""  # the text that suggests this relation


class PopulateResult(BaseModel):
    """填充结果"""

    entities_found: int = 0
    entities_stored: int = 0
    relations_found: int = 0
    relations_stored: int = 0
    new_concepts: list[str] = Field(default_factory=list)
    new_individuals: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 关系类型映射
# ---------------------------------------------------------------------------

# 将启发式提取的关系类型映射到本体的 RelationType 枚举
_RELATION_TYPE_MAP: dict[str, RelationType] = {
    "uses": RelationType.RELATED_TO,
    "depends_on": RelationType.RELATED_TO,
    "part_of": RelationType.PART_OF,
    "is_a": RelationType.IS_A,
    "related_to": RelationType.RELATED_TO,
    "causes": RelationType.CAUSES,
    "has_property": RelationType.HAS_PROPERTY,
    "instance_of": RelationType.INSTANCE_OF,
}


# ---------------------------------------------------------------------------
# 实体提取器
# ---------------------------------------------------------------------------


class EntityExtractor:
    """从文本中提取实体

    使用正则模式匹配常见实体类型（文件路径、URL、技术术语等），
    同时通过启发式规则识别 CamelCase 类名和通用实体。
    """

    # Built-in patterns for common entity types
    ENTITY_PATTERNS: dict[str, str] = {
        "file_path": r"[\w/\\]+\.\w+",
        "url": r"https?://\S+",
        "email": r"\b\w+@\w+\.\w+\b",
        "version": r"\bv?\d+\.\d+\.\d+\b",
        "tech_term": (
            r"\b(Python|JavaScript|TypeScript|Docker|Kubernetes"
            r"|LanceDB|FastAPI|Pydantic|asyncio|pytest"
            r"|Redis|PostgreSQL|MongoDB|GraphQL|Rust|Go|Java"
            r"|React|Vue|Angular|Node\.js|Django|Flask|SQLAlchemy)\b"
        ),
        "class_name": r"\b[A-Z][a-zA-Z]{2,}\b",  # CamelCase words (3+ chars)
    }

    # Stop words that should not be treated as class names
    _STOP_WORDS: set[str] = {
        "The",
        "This",
        "That",
        "These",
        "Those",
        "What",
        "When",
        "Where",
        "Which",
        "While",
        "With",
        "From",
        "Have",
        "Has",
        "Had",
        "Been",
        "Being",
        "Would",
        "Could",
        "Should",
        "Will",
        "Can",
        "May",
        "Might",
        "Must",
        "Shall",
        "Not",
        "But",
        "And",
        "For",
        "Nor",
        "Yet",
        "So",
        "Are",
        "Was",
        "Were",
        "Did",
        "Does",
        "Done",
        "Doesn",
        "Didn",
        "Isn",
        "Aren",
        "Wasn",
        "Weren",
        "Hasn",
        "Haven",
        "Hadn",
        "Each",
        "Every",
        "Some",
        "Any",
        "None",
        "All",
        "Both",
        "Most",
        "Many",
        "Few",
        "Several",
        "Other",
        "Another",
        "However",
        "Therefore",
        "Furthermore",
        "Moreover",
        "Otherwise",
        "Instead",
        "Finally",
        "First",
        "Second",
        "Third",
        "New",
        "Old",
        "Big",
        "Small",
        "Good",
        "Bad",
        "High",
        "Low",
        "Long",
        "Short",
        "Fast",
        "Slow",
        "Hard",
        "Easy",
        "True",
        "False",
        "Note",
        "See",
        "Also",
        "Just",
        "Only",
        "Even",
        "Still",
        "Already",
        "Here",
        "There",
        "Now",
        "Then",
        "After",
        "Before",
        "Since",
        "Until",
        "Above",
        "Below",
        "Between",
        "Under",
        "Over",
        "Let",
        "Set",
        "Get",
        "Run",
        "Use",
        "Make",
        "Take",
        "Give",
        "Put",
        "Keep",
        "Try",
        "Call",
        "Ask",
        "Tell",
        "Show",
        "Work",
        "Need",
        "Want",
        "Like",
        "Look",
        "Find",
        "Think",
        "Know",
        "Come",
        "Go",
        "See",
        "Take",
        "Make",
        "Say",
        "Back",
    }

    def extract(self, text: str) -> list[Entity]:
        """Extract all entities from text.

        Args:
            text: Input text to extract entities from.

        Returns:
            List of extracted entities, deduplicated by name.
        """
        entities_map: dict[str, Entity] = {}

        # Extract entities by pattern type (order matters: more specific first)
        for entity_type in ("url", "email", "version", "tech_term", "file_path", "class_name"):
            pattern = self.ENTITY_PATTERNS[entity_type]
            for match in re.finditer(pattern, text):
                name = match.group(0).strip()
                if not name or len(name) < 2:
                    continue

                # Filter stop words for class_name type
                if entity_type == "class_name" and name in self._STOP_WORDS:
                    continue

                # Skip if already captured by a more specific pattern
                if name in entities_map:
                    continue

                # Extract surrounding context (up to 80 chars around the match)
                start = max(0, match.start() - 40)
                end = min(len(text), match.end() + 40)
                context = text[start:end].replace("\n", " ").strip()

                confidence = self._compute_confidence(entity_type, name)
                entities_map[name] = Entity(
                    name=name,
                    entity_type=entity_type,
                    confidence=confidence,
                    context=context,
                )

        # Extract general entities: quoted strings and important nouns
        for match in re.finditer(r'"([^"]{2,64})"', text):
            name = match.group(1).strip()
            if name and name not in entities_map:
                start = max(0, match.start() - 40)
                end = min(len(text), match.end() + 40)
                context = text[start:end].replace("\n", " ").strip()
                entities_map[name] = Entity(
                    name=name,
                    entity_type="general",
                    confidence=0.6,
                    context=context,
                )

        logger.debug(f"实体提取完成: {len(entities_map)} 个实体")
        return list(entities_map.values())

    @staticmethod
    def _compute_confidence(entity_type: str, name: str) -> float:
        """根据实体类型和名称计算置信度"""
        if entity_type == "tech_term":
            return 0.95  # 精确匹配，高置信度
        if entity_type == "url":
            return 0.95
        if entity_type == "email":
            return 0.95
        if entity_type == "version":
            return 0.9
        if entity_type == "file_path":
            # 文件路径含有目录分隔符时更可信
            if "/" in name or "\\" in name:
                return 0.85
            return 0.7
        if entity_type == "class_name":
            # 越长的 CamelCase 词越可能是类名
            if len(name) >= 6:
                return 0.75
            return 0.6
        return 0.5


# ---------------------------------------------------------------------------
# 关系提取器
# ---------------------------------------------------------------------------


class RelationExtractor:
    """从文本中提取实体关系

    使用启发式规则匹配常见的关系模式（依赖、使用、组成等），
    并基于共现关系推断一般关联。
    """

    # 关系模式：(正则模式, 关系类型, 置信度)
    _PATTERNS: list[tuple[str, str, float]] = [
        # A uses / uses B
        (r"(\w[\w\s]*?)\s+uses?\s+(\w[\w\s]*?)(?:[,.]|$)", "uses", 0.8),
        (r"use\s+(\w[\w\s]*?)\s+(?:for|to)\s+(\w[\w\s]*?)(?:[,.]|$)", "uses", 0.7),
        # A depends on / requires B
        (
            r"(\w[\w\s]*?)\s+(?:depends?\s+on|requires?)\s+(\w[\w\s]*?)(?:[,.]|$)",
            "depends_on",
            0.85,
        ),
        # A is part of B / A is a B
        (r"(\w[\w\s]*?)\s+is\s+part\s+of\s+(\w[\w\s]*?)(?:[,.]|$)", "part_of", 0.85),
        (r"(\w[\w\s]*?)\s+is\s+a[n]?\s+(\w[\w\s]*?)(?:[,.]|$)", "is_a", 0.8),
        # A extends / inherits from B
        (r"(\w[\w\s]*?)\s+(?:extends?|inherits?\s+from)\s+(\w[\w\s]*?)(?:[,.]|$)", "is_a", 0.85),
        # A implements B
        (r"(\w[\w\s]*?)\s+implements?\s+(\w[\w\s]*?)(?:[,.]|$)", "is_a", 0.8),
        # A contains / includes B
        (r"(\w[\w\s]*?)\s+(?:contains?|includes?)\s+(\w[\w\s]*?)(?:[,.]|$)", "part_of", 0.75),
        # A causes / leads to B
        (r"(\w[\w\s]*?)\s+(?:causes?|leads?\s+to)\s+(\w[\w\s]*?)(?:[,.]|$)", "causes", 0.7),
        # A and B (co-occurrence, lower confidence)
        (r"\b(\w+)\s+and\s+(\w+)\b", "related_to", 0.4),
    ]

    def extract(self, text: str, entities: list[Entity]) -> list[Relation]:
        """Extract relationships between entities.

        Args:
            text: Source text.
            entities: Previously extracted entities.

        Returns:
            List of extracted relations, deduplicated.
        """
        entity_names = {e.name for e in entities}
        relations_map: dict[tuple[str, str, str], Relation] = {}

        # Apply pattern-based extraction
        for pattern_str, rel_type, confidence in self._PATTERNS:
            for match in re.finditer(pattern_str, text, re.IGNORECASE):
                source = match.group(1).strip()
                target = match.group(2).strip()

                if not source or not target or source == target:
                    continue

                # Normalize entity names to match extracted entities
                source_norm = self._find_matching_entity(source, entity_names)
                target_norm = self._find_matching_entity(target, entity_names)

                if not source_norm or not target_norm:
                    continue

                # Dedup key
                key = (source_norm, target_norm, rel_type)
                if key in relations_map:
                    continue

                # Extract evidence text
                start = max(0, match.start() - 30)
                end = min(len(text), match.end() + 30)
                evidence = text[start:end].replace("\n", " ").strip()

                relations_map[key] = Relation(
                    source=source_norm,
                    target=target_norm,
                    relation_type=rel_type,
                    confidence=confidence,
                    evidence=evidence,
                )

        # Sentence-level co-occurrence: entities in the same sentence
        self._extract_sentence_relations(text, entities, relations_map)

        logger.debug(f"关系提取完成: {len(relations_map)} 个关系")
        return list(relations_map.values())

    @staticmethod
    def _find_matching_entity(name: str, entity_names: set[str]) -> Optional[str]:
        """尝试将提取的名称匹配到已知实体名称。

        支持精确匹配和包含匹配（忽略大小写）。
        """
        # Exact match
        if name in entity_names:
            return name

        # Case-insensitive match
        name_lower = name.lower()
        for entity_name in entity_names:
            if entity_name.lower() == name_lower:
                return entity_name

        # Containment match: check if name is part of an entity or vice versa
        for entity_name in entity_names:
            if name_lower in entity_name.lower() or entity_name.lower() in name_lower:
                return entity_name

        return None

    @staticmethod
    def _extract_sentence_relations(
        text: str,
        entities: list[Entity],
        relations_map: dict[tuple[str, str, str], Relation],
    ) -> None:
        """基于句子共现提取一般关联关系（较低置信度）"""
        sentences = re.split(r"[.!?\n]+", text)
        entity_names = {e.name for e in entities}

        for sentence in sentences:
            sentence_stripped = sentence.strip()
            if not sentence_stripped:
                continue

            # Find which entities appear in this sentence
            present: list[str] = []
            for name in entity_names:
                if name.lower() in sentence_stripped.lower():
                    present.append(name)

            # Create related_to relations for co-occurring entities
            for i in range(len(present)):
                for j in range(i + 1, len(present)):
                    src, tgt = present[i], present[j]
                    key_fwd = (src, tgt, "related_to")
                    key_rev = (tgt, src, "related_to")
                    if key_fwd not in relations_map and key_rev not in relations_map:
                        relations_map[key_fwd] = Relation(
                            source=src,
                            target=tgt,
                            relation_type="related_to",
                            confidence=0.35,
                            evidence=sentence_stripped[:120],
                        )


# ---------------------------------------------------------------------------
# 自动填充器
# ---------------------------------------------------------------------------


class AutoPopulator:
    """自动从文本中提取实体和关系，填充本体图谱

    编排 EntityExtractor 和 RelationExtractor 的工作流程，
    并将提取结果存入 OntologyEngine，支持去重和概念推断。
    """

    # 实体类型到本体概念名称的映射
    _ENTITY_TYPE_CONCEPTS: dict[str, str] = {
        "file_path": "File",
        "url": "URL",
        "email": "Email",
        "version": "Version",
        "tech_term": "Technology",
        "class_name": "Class",
        "general": "Entity",
    }

    def __init__(self, ontology: OntologyEngine):
        self.ontology = ontology
        self.entity_extractor = EntityExtractor()
        self.relation_extractor = RelationExtractor()
        self._entity_cache: dict[str, str] = {}  # name -> individual_id

        # 预填充缓存：从本体中已有的个体建立索引
        for name, ind_id in ontology._name_to_individual.items():
            self._entity_cache[name] = ind_id

    async def populate_from_text(
        self,
        text: str,
        source: str = "conversation",
    ) -> PopulateResult:
        """从文本中提取实体和关系，存入本体。

        Args:
            text: 输入文本。
            source: 来源标识（记录到 metadata 中）。

        Returns:
            填充结果统计。
        """
        result = PopulateResult()

        # Step 1: 提取实体
        entities = self.entity_extractor.extract(text)
        result.entities_found = len(entities)

        # Step 2: 提取关系
        relations = self.relation_extractor.extract(text, entities)
        result.relations_found = len(relations)

        # Step 3: 存储实体（去重）
        for entity in entities:
            stored, individual_id, is_new = await self._store_entity(entity, source)
            if stored:
                result.entities_stored += 1
            if is_new:
                result.new_individuals.append(entity.name)

        # Step 4: 存储关系
        for relation in relations:
            stored, is_new_relation = await self._store_relation(relation, source)
            if stored:
                result.relations_stored += 1

        logger.info(
            f"填充完成: 实体({result.entities_found}发现/{result.entities_stored}存储), "
            f"关系({result.relations_found}发现/{result.relations_stored}存储), "
            f"新概念={len(result.new_concepts)}, 新个体={len(result.new_individuals)}"
        )
        return result

    async def populate_from_conversation(
        self,
        turns: list[dict],
    ) -> PopulateResult:
        """从对话历史中批量提取。

        Args:
            turns: 对话轮次列表，每个 dict 至少包含 "content" 字段。
                   可选字段: "role", "timestamp"。

        Returns:
            合并的填充结果统计。
        """
        combined = PopulateResult()
        all_new_concepts: set[str] = set()
        all_new_individuals: set[str] = set()

        for turn in turns:
            content = turn.get("content", "")
            if not content or not isinstance(content, str):
                continue

            role = turn.get("role", "unknown")
            source = f"conversation:{role}"

            result = await self.populate_from_text(content, source=source)

            combined.entities_found += result.entities_found
            combined.entities_stored += result.entities_stored
            combined.relations_found += result.relations_found
            combined.relations_stored += result.relations_stored
            all_new_concepts.update(result.new_concepts)
            all_new_individuals.update(result.new_individuals)

        combined.new_concepts = sorted(all_new_concepts)
        combined.new_individuals = sorted(all_new_individuals)

        logger.info(
            f"对话批量填充完成: {len(turns)} 轮对话, "
            f"实体({combined.entities_found}/{combined.entities_stored}), "
            f"关系({combined.relations_found}/{combined.relations_stored})"
        )
        return combined

    # ------------------------------------------------------------------
    # 内部方法：实体存储
    # ------------------------------------------------------------------

    async def _store_entity(
        self,
        entity: Entity,
        source: str,
    ) -> tuple[bool, str, bool]:
        """存储单个实体到本体，支持去重。

        Returns:
            (是否存储成功, individual_id, 是否为新创建)
        """
        name_key = entity.name.lower()

        # 检查缓存（去重）
        if name_key in self._entity_cache:
            individual_id = self._entity_cache[name_key]
            # 更新已有个体的 metadata
            self._update_individual_metadata(individual_id, entity, source)
            return True, individual_id, False

        # 确保对应的本体概念存在
        concept_id = self._ensure_concept(entity.entity_type)
        if concept_id and concept_id not in [c for c in self.ontology._concepts]:
            # 如果是新概念，记录到结果中
            concept = self.ontology._concepts.get(concept_id)
            if concept:
                pass  # 概念已在 _ensure_concept 中添加

        # 创建个体实例
        concept_ids = [concept_id] if concept_id else []
        individual = Individual(
            name=entity.name,
            concept_ids=concept_ids,
            properties={
                "entity_type": entity.entity_type,
                "confidence": entity.confidence,
            },
            metadata={
                "source": source,
                "context": entity.context,
                "auto_extracted": True,
            },
        )

        individual_id = self.ontology.add_individual(individual)
        self._entity_cache[name_key] = individual_id

        logger.debug(
            f"存储实体: {entity.name} (type={entity.entity_type}, "
            f"confidence={entity.confidence:.2f})"
        )
        return True, individual_id, True

    def _update_individual_metadata(
        self,
        individual_id: str,
        entity: Entity,
        source: str,
    ) -> None:
        """更新已有个体的 metadata（如来源、上下文）"""
        individual = self.ontology._individuals.get(individual_id)
        if not individual:
            return

        # 更新 confidence 取较高值
        current_conf = individual.properties.get("confidence", 0.0)
        if entity.confidence > current_conf:
            individual.properties["confidence"] = entity.confidence

        # 追加来源信息
        sources = individual.metadata.get("sources", [])
        if source not in sources:
            sources.append(source)
            individual.metadata["sources"] = sources

        # 更新最后修改时间
        from datetime import datetime

        individual.last_updated = datetime.now()

    def _ensure_concept(self, entity_type: str) -> Optional[str]:
        """确保实体类型对应的本体概念存在，不存在则创建。

        Returns:
            概念 ID
        """
        concept_name = self._ENTITY_TYPE_CONCEPTS.get(entity_type, "Entity")

        # 检查概念是否已存在
        existing_id = self.ontology._name_to_concept.get(concept_name.lower())
        if existing_id:
            return existing_id

        # 创建新概念
        # 建立层级关系：Class -> Entity, File -> Entity, etc.
        parent_ids: list[str] = []
        if concept_name != "Entity":
            entity_concept_id = self._ensure_concept("general")
            if entity_concept_id:
                parent_ids.append(entity_concept_id)

        description = self._concept_descriptions.get(
            concept_name, f"自动创建的 {concept_name} 概念"
        )

        concept = Concept(
            name=concept_name,
            description=description,
            parent_concepts=parent_ids,
            metadata={"auto_created": True},
        )

        concept_id = self.ontology.add_concept(concept)
        logger.debug(f"创建概念: {concept_name} (id={concept_id})")
        return concept_id

    # 概念描述映射
    _concept_descriptions: dict[str, str] = {
        "Entity": "通用实体基类",
        "File": "文件路径实体",
        "URL": "网址链接实体",
        "Email": "电子邮件实体",
        "Version": "版本号实体",
        "Technology": "技术术语/工具实体",
        "Class": "类名（CamelCase 标识符）实体",
    }

    # ------------------------------------------------------------------
    # 内部方法：关系存储
    # ------------------------------------------------------------------

    async def _store_relation(
        self,
        relation: Relation,
        source: str,
    ) -> tuple[bool, bool]:
        """存储单个关系到本体。

        Returns:
            (是否存储成功, 是否为新关系)
        """
        # 查找源和目标个体的 ID
        source_id = self._entity_cache.get(relation.source.lower())
        target_id = self._entity_cache.get(relation.target.lower())

        if not source_id or not target_id:
            logger.debug(
                f"跳过关系: {relation.source} -> {relation.target} "
                f"(实体未找到: source={'有' if source_id else '无'}, "
                f"target={'有' if target_id else '无'})"
            )
            return False, False

        # 确保关系定义存在
        relation_def_id = self._ensure_relation_definition(relation.relation_type)

        # 检查是否已存在相同关系实例（避免重复）
        if self._relation_exists(source_id, target_id, relation_def_id):
            return False, False

        # 创建关系实例
        rel_instance = RelationInstance(
            relation_id=relation_def_id,
            source_id=source_id,
            target_id=target_id,
            weight=relation.confidence,
            metadata={
                "source": source,
                "evidence": relation.evidence,
                "auto_extracted": True,
                "original_type": relation.relation_type,
            },
        )

        self.ontology.add_relation_instance(rel_instance)

        logger.debug(
            f"存储关系: {relation.source} --[{relation.relation_type}]--> "
            f"{relation.target} (confidence={relation.confidence:.2f})"
        )
        return True, True

    def _ensure_relation_definition(self, relation_type: str) -> str:
        """确保关系定义存在，不存在则创建。

        Returns:
            关系定义 ID
        """
        # 查找已有定义
        for rel_def in self.ontology._relation_defs.values():
            if rel_def.name == relation_type:
                return rel_def.relation_id

        # 创建新关系定义
        ontology_type = _RELATION_TYPE_MAP.get(relation_type, RelationType.CUSTOM)
        rel_def = RelationDefinition(
            name=relation_type,
            relation_type=ontology_type,
            metadata={"auto_created": True},
        )

        rel_def_id = self.ontology.add_relation_definition(rel_def)
        logger.debug(f"创建关系定义: {relation_type} (id={rel_def_id})")
        return rel_def_id

    def _relation_exists(
        self,
        source_id: str,
        target_id: str,
        relation_def_id: str,
    ) -> bool:
        """检查是否已存在相同的关系实例"""
        for rel_inst in self.ontology._relation_instances.values():
            if (
                rel_inst.source_id == source_id
                and rel_inst.target_id == target_id
                and rel_inst.relation_id == relation_def_id
            ):
                return True
        return False
