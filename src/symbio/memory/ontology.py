"""本体引擎 - T-Box/A-Box 分离、零 Token 图推理"""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

logger = get_logger("ontology")


# ---------------------------------------------------------------------------
# Pydantic 数据模型
# ---------------------------------------------------------------------------


class EntityType(str, Enum):
    """实体类型"""

    CONCEPT = "concept"  # 概念类
    INDIVIDUAL = "individual"  # 个体实例
    PROPERTY = "property"  # 属性
    RELATION = "relation"  # 关系


class RelationType(str, Enum):
    """关系类型"""

    IS_A = "is_a"  # 继承关系 (A is_a B)
    PART_OF = "part_of"  # 组成关系 (A part_of B)
    HAS_PROPERTY = "has_property"  # 属性关系 (A has_property P)
    INSTANCE_OF = "instance_of"  # 实例关系 (A instance_of C)
    RELATED_TO = "related_to"  # 一般关联
    CAUSES = "causes"  # 因果关系
    TEMPORAL = "temporal"  # 时序关系
    SPATIAL = "spatial"  # 空间关系
    CUSTOM = "custom"  # 自定义关系


class InferenceRule(str, Enum):
    """推理规则"""

    TRANSITIVITY = "transitivity"  # 传递性 (A is_a B, B is_a C => A is_a C)
    INHERITANCE = "inheritance"  # 属性继承
    COMPOSITION = "composition"  # 组合推理
    SYMMETRY = "symmetry"  # 对称性
    INVERSE = "inverse"  # 反向关系


# T-Box: 概念层（Schema）
class Concept(BaseModel):
    """概念定义（T-Box）"""

    concept_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str  # 概念名称
    description: str = ""  # 概念描述
    parent_concepts: list[str] = Field(default_factory=list)  # 父概念 ID 列表
    properties: list[str] = Field(default_factory=list)  # 属性定义 ID 列表
    constraints: dict[str, Any] = Field(default_factory=dict)  # 约束条件
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)


class PropertyDefinition(BaseModel):
    """属性定义（T-Box）"""

    property_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str  # 属性名称
    domain: str = ""  # 定义域（概念名称）
    range_type: str = "string"  # 值域类型
    is_required: bool = False  # 是否必填
    is_unique: bool = False  # 是否唯一
    default_value: Any = None  # 默认值
    constraints: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RelationDefinition(BaseModel):
    """关系定义（T-Box）"""

    relation_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str  # 关系名称
    relation_type: RelationType = RelationType.CUSTOM
    domain_concept: str = ""  # 定义域概念
    range_concept: str = ""  # 值域概念
    is_transitive: bool = False  # 是否可传递
    is_symmetric: bool = False  # 是否对称
    inverse_relation: str = ""  # 反向关系名称
    constraints: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


# A-Box: 实例层（Data）
class Individual(BaseModel):
    """个体实例（A-Box）"""

    individual_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str  # 实例名称
    concept_ids: list[str] = Field(default_factory=list)  # 所属概念 ID 列表
    properties: dict[str, Any] = Field(default_factory=dict)  # 属性值
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)
    last_updated: datetime = Field(default_factory=datetime.now)


class RelationInstance(BaseModel):
    """关系实例（A-Box）"""

    instance_id: str = Field(default_factory=lambda: str(uuid4()))
    relation_id: str  # 关系定义 ID
    source_id: str  # 源实体 ID
    target_id: str  # 目标实体 ID
    weight: float = 1.0  # 关系权重
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)


# 图结构
class GraphNode(BaseModel):
    """图节点"""

    node_id: str
    node_type: EntityType = EntityType.CONCEPT
    label: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    """图边"""

    edge_id: str = Field(default_factory=lambda: str(uuid4()))
    source_id: str
    target_id: str
    relation_type: RelationType = RelationType.CUSTOM
    weight: float = 1.0
    label: str = ""


class SubGraph(BaseModel):
    """子图查询结果"""

    subgraph_id: str = Field(default_factory=lambda: str(uuid4()))
    center_node_id: str  # 中心节点
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    depth: int = 0  # 查询深度
    token_estimate: int = 0  # 预估 Token 数（用于零 Token 控制）


class QueryResult(BaseModel):
    """查询结果"""

    result_id: str = Field(default_factory=lambda: str(uuid4()))
    query: str
    results: list[dict[str, Any]] = Field(default_factory=list)
    subgraph: Optional[SubGraph] = None
    token_count: int = 0  # 结果占用的 Token 数
    inference_applied: list[InferenceRule] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)


class OntologyConfig(BaseModel):
    """本体引擎配置"""

    max_depth: int = 3  # 最大查询深度
    max_nodes_per_query: int = 50  # 每次查询最大节点数
    enable_inference: bool = True  # 启用推理
    enable_inheritance: bool = True  # 启用属性继承
    token_budget: int = 2000  # Token 预算（零 Token 控制）
    persist_path: str = ""  # 持久化路径
    auto_save: bool = True  # 自动保存


# ---------------------------------------------------------------------------
# 本体引擎
# ---------------------------------------------------------------------------


class OntologyEngine:
    """本体引擎

    核心能力：
    1. T-Box/A-Box 分离 - 概念层与实例层分离管理
    2. 零 Token 图推理 - 在 Token 预算内完成图推理
    3. 属性继承 - 子概念自动继承父概念属性
    4. 传递性推理 - 基于关系传递性推导新知识

    T-Box（概念层）：
    - 概念定义（类）
    - 属性定义
    - 关系定义

    A-Box（实例层）：
    - 个体实例
    - 关系实例

    Usage:
        engine = OntologyEngine()
        engine.add_concept(Concept(name="Animal"))
        engine.add_concept(Concept(name="Dog", parent_concepts=[animal_id]))
        engine.add_individual(Individual(name="Buddy", concept_ids=[dog_id]))
        result = engine.query("what is Buddy?")
    """

    def __init__(self, config: Optional[OntologyConfig] = None):
        self._config = config or OntologyConfig()

        # T-Box 存储
        self._concepts: dict[str, Concept] = {}
        self._properties: dict[str, PropertyDefinition] = {}
        self._relation_defs: dict[str, RelationDefinition] = {}

        # A-Box 存储
        self._individuals: dict[str, Individual] = {}
        self._relation_instances: dict[str, RelationInstance] = {}

        # 索引
        self._name_to_concept: dict[str, str] = {}  # name -> concept_id
        self._name_to_individual: dict[str, str] = {}  # name -> individual_id
        self._concept_individuals: dict[str, list[str]] = {}  # concept_id -> [individual_ids]

        # 推理缓存
        self._inference_cache: dict[str, list[str]] = {}

        logger.info(
            f"OntologyEngine 创建: max_depth={self._config.max_depth}, "
            f"token_budget={self._config.token_budget}"
        )

    # ------------------------------------------------------------------
    # T-Box 操作
    # ------------------------------------------------------------------

    def add_concept(self, concept: Concept) -> str:
        """添加概念定义

        Args:
            concept: 概念定义

        Returns:
            概念 ID
        """
        self._concepts[concept.concept_id] = concept
        self._name_to_concept[concept.name.lower()] = concept.concept_id

        # 清除推理缓存
        self._inference_cache.clear()

        logger.debug(f"添加概念: {concept.name} (id={concept.concept_id})")
        return concept.concept_id

    def add_property_definition(self, prop: PropertyDefinition) -> str:
        """添加属性定义

        Args:
            prop: 属性定义

        Returns:
            属性 ID
        """
        self._properties[prop.property_id] = prop

        # 关联到概念
        if prop.domain:
            concept_id = self._name_to_concept.get(prop.domain.lower())
            if concept_id and concept_id in self._concepts:
                self._concepts[concept_id].properties.append(prop.property_id)

        logger.debug(f"添加属性定义: {prop.name} (id={prop.property_id})")
        return prop.property_id

    def add_relation_definition(self, rel_def: RelationDefinition) -> str:
        """添加关系定义

        Args:
            rel_def: 关系定义

        Returns:
            关系定义 ID
        """
        self._relation_defs[rel_def.relation_id] = rel_def
        logger.debug(f"添加关系定义: {rel_def.name} (id={rel_def.relation_id})")
        return rel_def.relation_id

    # ------------------------------------------------------------------
    # A-Box 操作
    # ------------------------------------------------------------------

    def add_individual(self, individual: Individual) -> str:
        """添加个体实例

        Args:
            individual: 个体实例

        Returns:
            个体 ID
        """
        self._individuals[individual.individual_id] = individual
        self._name_to_individual[individual.name.lower()] = individual.individual_id

        # 更新概念-实例索引
        for concept_id in individual.concept_ids:
            if concept_id not in self._concept_individuals:
                self._concept_individuals[concept_id] = []
            self._concept_individuals[concept_id].append(individual.individual_id)

        logger.debug(f"添加个体: {individual.name} (id={individual.individual_id})")
        return individual.individual_id

    def add_relation_instance(self, rel_instance: RelationInstance) -> str:
        """添加关系实例

        Args:
            rel_instance: 关系实例

        Returns:
            关系实例 ID
        """
        self._relation_instances[rel_instance.instance_id] = rel_instance
        logger.debug(
            f"添加关系实例: {rel_instance.source_id} -> {rel_instance.target_id} "
            f"(relation={rel_instance.relation_id})"
        )
        return rel_instance.instance_id

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def query(
        self,
        query_text: str,
        max_results: int = 10,
    ) -> QueryResult:
        """执行本体查询

        支持的查询模式：
        1. 实体查询："what is X?" / "X 是什么？"
        2. 关系查询："X is_a Y?" / "X 和 Y 的关系？"
        3. 属性查询："X 的属性" / "properties of X"
        4. 子图查询："X 的邻居" / "neighbors of X"

        Args:
            query_text: 查询文本
            max_results: 最大结果数

        Returns:
            查询结果
        """
        logger.debug(f"执行查询: {query_text}")

        query_lower = query_text.lower().strip()
        results: list[dict[str, Any]] = []
        inference_applied: list[InferenceRule] = []
        subgraph: Optional[SubGraph] = None

        # 解析查询意图
        if "是什么" in query_lower or "what is" in query_lower:
            results, subgraph = self._query_entity(query_text, max_results)
        elif "关系" in query_lower or "relation" in query_lower:
            results = self._query_relation(query_text)
        elif "属性" in query_lower or "property" in query_lower or "properties" in query_lower:
            results = self._query_properties(query_text)
        elif "邻居" in query_lower or "neighbor" in query_lower:
            center_id = self._extract_entity_id(query_text)
            if center_id:
                subgraph = self.get_neighbors(center_id)
                results = [{"type": "subgraph", "nodes": len(subgraph.nodes)}]
        else:
            # 通用搜索
            results, subgraph = self._query_general(query_text, max_results)

        # 应用推理
        if self._config.enable_inference and results:
            inferred, rules = self._apply_inference(results)
            results.extend(inferred)
            inference_applied.extend(rules)

        # Token 控制
        token_count = self._estimate_result_tokens(results)

        result = QueryResult(
            query=query_text,
            results=results[:max_results],
            subgraph=subgraph,
            token_count=token_count,
            inference_applied=inference_applied,
        )

        logger.info(
            f"查询完成: results={len(results)}, "
            f"tokens={token_count}, "
            f"inference_rules={len(inference_applied)}"
        )
        return result

    def _query_entity(
        self,
        query_text: str,
        max_results: int,
    ) -> tuple[list[dict[str, Any]], Optional[SubGraph]]:
        """实体查询"""
        entity_name = self._extract_entity_name(query_text)
        if not entity_name:
            return [], None

        results: list[dict[str, Any]] = []

        # 查找概念
        concept_id = self._name_to_concept.get(entity_name.lower())
        if concept_id:
            concept = self._concepts[concept_id]
            results.append(
                {
                    "type": "concept",
                    "id": concept.concept_id,
                    "name": concept.name,
                    "description": concept.description,
                    "parent_concepts": [
                        self._concepts[pid].name
                        for pid in concept.parent_concepts
                        if pid in self._concepts
                    ],
                }
            )

        # 查找个体
        individual_id = self._name_to_individual.get(entity_name.lower())
        if individual_id:
            individual = self._individuals[individual_id]
            results.append(
                {
                    "type": "individual",
                    "id": individual.individual_id,
                    "name": individual.name,
                    "concepts": [
                        self._concepts[cid].name
                        for cid in individual.concept_ids
                        if cid in self._concepts
                    ],
                    "properties": individual.properties,
                }
            )

        # 生成子图
        subgraph = None
        if results:
            center_id = results[0].get("id", "")
            if center_id:
                subgraph = self.get_neighbors(center_id, depth=1)

        return results, subgraph

    def _query_relation(self, query_text: str) -> list[dict[str, Any]]:
        """关系查询"""
        results: list[dict[str, Any]] = []

        # 查找所有关系实例
        for rel_inst in self._relation_instances.values():
            rel_def = self._relation_defs.get(rel_inst.relation_id)
            if not rel_def:
                continue

            source = self._individuals.get(rel_inst.source_id)
            target = self._individuals.get(rel_inst.target_id)

            if source and target:
                results.append(
                    {
                        "type": "relation",
                        "relation": rel_def.name,
                        "source": source.name,
                        "target": target.name,
                        "weight": rel_inst.weight,
                    }
                )

        return results

    def _query_properties(self, query_text: str) -> list[dict[str, Any]]:
        """属性查询"""
        entity_name = self._extract_entity_name(query_text)
        if not entity_name:
            return []

        results: list[dict[str, Any]] = []

        # 查找个体属性
        individual_id = self._name_to_individual.get(entity_name.lower())
        if individual_id:
            individual = self._individuals[individual_id]
            for prop_name, prop_value in individual.properties.items():
                results.append(
                    {
                        "type": "property",
                        "entity": individual.name,
                        "property": prop_name,
                        "value": prop_value,
                    }
                )

            # 属性继承
            if self._config.enable_inheritance:
                for concept_id in individual.concept_ids:
                    inherited = self._get_inherited_properties(concept_id)
                    for prop_name, prop_value in inherited.items():
                        if prop_name not in individual.properties:
                            results.append(
                                {
                                    "type": "inherited_property",
                                    "entity": individual.name,
                                    "property": prop_name,
                                    "value": prop_value,
                                    "inherited_from": self._concepts.get(
                                        concept_id, Concept(name="unknown")
                                    ).name,
                                }
                            )

        return results

    def _query_general(
        self,
        query_text: str,
        max_results: int,
    ) -> tuple[list[dict[str, Any]], Optional[SubGraph]]:
        """通用搜索"""
        results: list[dict[str, Any]] = []
        query_lower = query_text.lower()

        # 搜索概念
        for concept in self._concepts.values():
            if query_lower in concept.name.lower() or query_lower in concept.description.lower():
                results.append(
                    {
                        "type": "concept",
                        "id": concept.concept_id,
                        "name": concept.name,
                        "description": concept.description,
                    }
                )

        # 搜索个体
        for individual in self._individuals.values():
            if query_lower in individual.name.lower():
                results.append(
                    {
                        "type": "individual",
                        "id": individual.individual_id,
                        "name": individual.name,
                        "concepts": [
                            self._concepts[cid].name
                            for cid in individual.concept_ids
                            if cid in self._concepts
                        ],
                    }
                )

        # 生成子图（如果有结果）
        subgraph = None
        if results:
            center_id = results[0].get("id", "")
            if center_id:
                subgraph = self.get_neighbors(center_id, depth=1)

        return results[:max_results], subgraph

    # ------------------------------------------------------------------
    # 图操作
    # ------------------------------------------------------------------

    def get_neighbors(
        self,
        node_id: str,
        depth: int = 1,
    ) -> SubGraph:
        """获取节点的邻居子图

        Args:
            node_id: 中心节点 ID
            depth: 查询深度

        Returns:
            子图
        """
        depth = min(depth, self._config.max_depth)
        visited_nodes: set[str] = set()
        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []

        # BFS 遍历
        queue: list[tuple[str, int]] = [(node_id, 0)]
        visited_nodes.add(node_id)

        while queue:
            current_id, current_depth = queue.pop(0)

            if current_depth >= depth:
                continue

            if len(nodes) >= self._config.max_nodes_per_query:
                break

            # 添加当前节点
            node = self._create_graph_node(current_id)
            if node:
                nodes.append(node)

            # 查找相关的关系实例
            for rel_inst in self._relation_instances.values():
                neighbor_id: Optional[str] = None

                if rel_inst.source_id == current_id:
                    neighbor_id = rel_inst.target_id
                elif rel_inst.target_id == current_id:
                    neighbor_id = rel_inst.source_id

                if neighbor_id and neighbor_id not in visited_nodes:
                    visited_nodes.add(neighbor_id)

                    # 添加边
                    rel_def = self._relation_defs.get(rel_inst.relation_id)
                    edge = GraphEdge(
                        source_id=rel_inst.source_id,
                        target_id=rel_inst.target_id,
                        relation_type=rel_def.relation_type if rel_def else RelationType.CUSTOM,
                        weight=rel_inst.weight,
                        label=rel_def.name if rel_def else "",
                    )
                    edges.append(edge)

                    # 加入队列继续遍历
                    queue.append((neighbor_id, current_depth + 1))

        token_estimate = self._estimate_subgraph_tokens(nodes, edges)

        subgraph = SubGraph(
            center_node_id=node_id,
            nodes=nodes,
            edges=edges,
            depth=depth,
            token_estimate=token_estimate,
        )

        logger.debug(
            f"子图查询: center={node_id}, depth={depth}, "
            f"nodes={len(nodes)}, edges={len(edges)}, "
            f"tokens={token_estimate}"
        )
        return subgraph

    def _create_graph_node(self, node_id: str) -> Optional[GraphNode]:
        """创建图节点"""
        # 尝试作为概念
        if node_id in self._concepts:
            concept = self._concepts[node_id]
            return GraphNode(
                node_id=node_id,
                node_type=EntityType.CONCEPT,
                label=concept.name,
                data={"description": concept.description},
            )

        # 尝试作为个体
        if node_id in self._individuals:
            individual = self._individuals[node_id]
            return GraphNode(
                node_id=node_id,
                node_type=EntityType.INDIVIDUAL,
                label=individual.name,
                data={"properties": individual.properties},
            )

        return None

    # ------------------------------------------------------------------
    # 推理引擎
    # ------------------------------------------------------------------

    def _apply_inference(
        self,
        results: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[InferenceRule]]:
        """应用推理规则

        Returns:
            (推理得出的新结果, 应用的推理规则)
        """
        inferred: list[dict[str, Any]] = []
        applied_rules: list[InferenceRule] = []

        for result in results:
            if result.get("type") == "individual":
                individual_id = result.get("id", "")
                if not individual_id:
                    continue

                # 传递性推理（通过 is_a 关系）
                transitive_concepts = self._infer_transitive_concepts(individual_id)
                if transitive_concepts:
                    inferred.append(
                        {
                            "type": "inferred_concept",
                            "individual": result.get("name", ""),
                            "inferred_concepts": transitive_concepts,
                            "rule": "transitivity",
                        }
                    )
                    if InferenceRule.TRANSITIVITY not in applied_rules:
                        applied_rules.append(InferenceRule.TRANSITIVITY)

                # 属性继承推理
                inherited_props = self._infer_inherited_properties(individual_id)
                if inherited_props:
                    inferred.append(
                        {
                            "type": "inferred_properties",
                            "individual": result.get("name", ""),
                            "inherited_properties": inherited_props,
                            "rule": "inheritance",
                        }
                    )
                    if InferenceRule.INHERITANCE not in applied_rules:
                        applied_rules.append(InferenceRule.INHERITANCE)

        return inferred, applied_rules

    def _infer_transitive_concepts(self, individual_id: str) -> list[str]:
        """传递性推理：推导个体所属的所有概念（包括间接继承）

        例如：Dog is_a Animal, Animal is_a LivingThing
        => Dog 的个体也属于 LivingThing
        """
        cache_key = f"transitive_{individual_id}"
        if cache_key in self._inference_cache:
            return self._inference_cache[cache_key]

        individual = self._individuals.get(individual_id)
        if not individual:
            return []

        all_concepts: set[str] = set()
        queue = list(individual.concept_ids)

        while queue:
            concept_id = queue.pop(0)
            if concept_id in all_concepts:
                continue

            concept = self._concepts.get(concept_id)
            if not concept:
                continue

            all_concepts.add(concept.name)

            # 遍历父概念
            for parent_id in concept.parent_concepts:
                if parent_id not in all_concepts:
                    queue.append(parent_id)

        # 移除直接概念（已经知道的）
        direct_concepts = {
            self._concepts[cid].name for cid in individual.concept_ids if cid in self._concepts
        }
        inferred = list(all_concepts - direct_concepts)

        self._inference_cache[cache_key] = inferred
        return inferred

    def _infer_inherited_properties(self, individual_id: str) -> dict[str, Any]:
        """属性继承推理：从父概念继承属性默认值"""
        individual = self._individuals.get(individual_id)
        if not individual:
            return {}

        inherited: dict[str, Any] = {}

        for concept_id in individual.concept_ids:
            concept = self._concepts.get(concept_id)
            if not concept:
                continue

            # 遍历概念的属性定义
            for prop_id in concept.properties:
                prop_def = self._properties.get(prop_id)
                if not prop_def:
                    continue

                # 如果个体没有该属性，继承默认值
                if (
                    prop_def.name not in individual.properties
                    and prop_def.default_value is not None
                ):
                    inherited[prop_def.name] = prop_def.default_value

        return inherited

    def _get_inherited_properties(self, concept_id: str) -> dict[str, Any]:
        """获取概念从父概念继承的属性定义"""
        inherited: dict[str, Any] = {}
        visited: set[str] = set()
        queue = [concept_id]

        while queue:
            current_id = queue.pop(0)
            if current_id in visited:
                continue
            visited.add(current_id)

            concept = self._concepts.get(current_id)
            if not concept:
                continue

            for prop_id in concept.properties:
                prop_def = self._properties.get(prop_id)
                if prop_def and prop_def.default_value is not None:
                    if prop_def.name not in inherited:
                        inherited[prop_def.name] = prop_def.default_value

            for parent_id in concept.parent_concepts:
                if parent_id not in visited:
                    queue.append(parent_id)

        return inherited

    # ------------------------------------------------------------------
    # Token 控制
    # ------------------------------------------------------------------

    def _estimate_result_tokens(self, results: list[dict[str, Any]]) -> int:
        """估算结果占用的 Token 数"""
        if not results:
            return 0

        # 简单估算：JSON 序列化后按字符数除以 4
        total_chars = sum(len(json.dumps(r, ensure_ascii=False)) for r in results)
        return total_chars // 4

    def _estimate_subgraph_tokens(
        self,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
    ) -> int:
        """估算子图占用的 Token 数"""
        node_chars = sum(
            len(node.label) + len(json.dumps(node.data, ensure_ascii=False)) for node in nodes
        )
        edge_chars = sum(len(edge.label) + 20 for edge in edges)
        return (node_chars + edge_chars) // 4

    def fits_token_budget(self, subgraph: SubGraph) -> bool:
        """检查子图是否在 Token 预算内

        Args:
            subgraph: 子图

        Returns:
            是否在预算内
        """
        return subgraph.token_estimate <= self._config.token_budget

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _extract_entity_name(self, text: str) -> str:
        """从查询文本中提取实体名称"""
        import re

        # 中文模式
        patterns = [
            r"(?:什么是|是什么|告诉我关于)\s*(.+?)(?:\s*的|$|\?)",
            r"(.+?)(?:\s*是什么|的属性|的邻居)",
        ]

        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1).strip()

        # 英文模式
        patterns_en = [
            r"what\s+is\s+(.+?)(?:\?|$)",
            r"(?:properties|neighbors?)\s+of\s+(.+?)(?:\?|$)",
        ]

        for pattern in patterns_en:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1).strip()

        return ""

    def _extract_entity_id(self, text: str) -> Optional[str]:
        """从查询文本中提取实体 ID"""
        name = self._extract_entity_name(text)
        if not name:
            return None

        # 尝试查找概念
        concept_id = self._name_to_concept.get(name.lower())
        if concept_id:
            return concept_id

        # 尝试查找个体
        individual_id = self._name_to_individual.get(name.lower())
        if individual_id:
            return individual_id

        return None

    # ------------------------------------------------------------------
    # 统计接口
    # ------------------------------------------------------------------

    def get_statistics(self) -> dict[str, Any]:
        """获取本体统计信息"""
        return {
            "tbox": {
                "concepts": len(self._concepts),
                "properties": len(self._properties),
                "relation_definitions": len(self._relation_defs),
            },
            "abox": {
                "individuals": len(self._individuals),
                "relation_instances": len(self._relation_instances),
            },
            "inference_cache_size": len(self._inference_cache),
        }

    def get_concept_hierarchy(self) -> dict[str, list[str]]:
        """获取概念层次结构

        Returns:
            {concept_name: [parent_concept_names]}
        """
        hierarchy: dict[str, list[str]] = {}
        for concept in self._concepts.values():
            parents = [
                self._concepts[pid].name for pid in concept.parent_concepts if pid in self._concepts
            ]
            hierarchy[concept.name] = parents
        return hierarchy

    def clear_inference_cache(self) -> None:
        """清除推理缓存"""
        self._inference_cache.clear()
        logger.debug("推理缓存已清除")

    # ------------------------------------------------------------------
    # 工厂方法
    # ------------------------------------------------------------------

    @classmethod
    def create_default(cls) -> OntologyEngine:
        """使用默认配置创建"""
        return cls(OntologyConfig())

    @classmethod
    def create_with_budget(cls, token_budget: int = 2000) -> OntologyEngine:
        """创建指定 Token 预算的引擎"""
        return cls(OntologyConfig(token_budget=token_budget))
