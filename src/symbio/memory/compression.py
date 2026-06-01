"""记忆压缩流水线 - 聚类、模式识别、规则提取、T-Box 注入、冷存储归档"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from symbio.memory.manager import (
    MemoryItem,
    MemoryManager,
    MemoryPriority,
    MemoryStatus,
    MemoryType,
)
from symbio.memory.ontology import (
    Concept,
    OntologyEngine,
    PropertyDefinition,
    RelationDefinition,
    RelationType,
)
from symbio.utils.logger import get_logger

logger = get_logger("memory_compression")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

class CompressionStats(BaseModel):
    """压缩统计"""
    original_count: int = 0          # 原始记忆数量
    compressed_count: int = 0        # 压缩后记忆数量
    rules_extracted: int = 0         # 提取的规则数
    clusters_formed: int = 0         # 形成的聚类数
    cold_archived: int = 0           # 冷存储归档数
    compression_ratio: float = 0.0   # 压缩比 (compressed / original)
    duration_seconds: float = 0.0    # 耗时
    created_at: datetime = Field(default_factory=datetime.now)

    @property
    def summary(self) -> str:
        return (
            f"压缩统计: {self.original_count} -> {self.compressed_count} "
            f"(比率 {self.compression_ratio:.2%}), "
            f"聚类 {self.clusters_formed}, 规则 {self.rules_extracted}, "
            f"归档 {self.cold_archived}, 耗时 {self.duration_seconds:.2f}s"
        )


@dataclass
class MemoryCluster:
    """记忆聚类"""
    cluster_id: str = field(default_factory=lambda: str(uuid4()))
    centroid: list[float] = field(default_factory=list)
    members: list[MemoryItem] = field(default_factory=list)
    theme: str = ""                       # 聚类主题（由模式识别填充）
    common_tags: list[str] = field(default_factory=list)
    avg_importance: float = 0.0

    @property
    def size(self) -> int:
        return len(self.members)


@dataclass
class ExtractedRule:
    """提取的规则"""
    rule_id: str = field(default_factory=lambda: str(uuid4()))
    name: str = ""
    description: str = ""
    source_cluster_ids: list[str] = field(default_factory=list)
    concept_name: str = ""               # 对应的 T-Box 概念名
    relation_name: str = ""              # 对应的 T-Box 关系名
    properties: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    example_contents: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

class CompressorConfig(BaseModel):
    """压缩器配置"""
    # 聚类参数
    n_clusters: int = 0                   # 聚类数，0 = 自动推断
    max_clusters: int = 20                # 最大聚类数
    min_cluster_size: int = 3             # 最小聚类大小
    embedding_dim: int = 0                # 嵌入维度，0 = 自动检测

    # 模式识别参数
    min_pattern_frequency: int = 2        # 最小模式出现频率
    tag_weight: float = 0.3               # 标签权重
    content_weight: float = 0.7           # 内容权重

    # 规则提取参数
    min_cluster_confidence: float = 0.5   # 最小聚类置信度
    max_rules_per_cluster: int = 3        # 每个聚类最大规则数

    # 冷存储参数
    cold_storage_memory_type: MemoryType = MemoryType.SEMANTIC
    archive_importance_threshold: float = 0.3  # 低于此重要性的原始记忆归档

    # 流水线控制
    search_batch_size: int = 500          # 每批搜索记忆数
    search_query: str = "*"               # 搜索查询（* 表示全部）


# ---------------------------------------------------------------------------
# 记忆压缩器
# ---------------------------------------------------------------------------

class MemoryCompressor:
    """记忆压缩流水线

    流水线阶段:
    1. clustering      - 将记忆按向量相似度聚类
    2. pattern_recognition - 在聚类中识别共同主题
    3. rule_extraction  - 将模式转化为结构化规则
    4. tbox_injection   - 将规则注入本体引擎 T-Box
    5. cold_storage     - 将原始记忆归档为冷存储

    Usage:
        compressor = MemoryCompressor(memory_manager, ontology_engine)
        stats = await compressor.run()
        print(stats.summary)
    """

    def __init__(
        self,
        memory_manager: MemoryManager,
        ontology_engine: Optional[OntologyEngine] = None,
        config: Optional[CompressorConfig] = None,
    ):
        self._manager = memory_manager
        self._ontology = ontology_engine or OntologyEngine.create_default()
        self._config = config or CompressorConfig()

        # 流水线状态
        self._memories: list[MemoryItem] = []
        self._clusters: list[MemoryCluster] = []
        self._patterns: list[dict[str, Any]] = []
        self._rules: list[ExtractedRule] = []

        logger.info("MemoryCompressor 创建")

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    async def run(self) -> CompressionStats:
        """执行完整的压缩流水线

        Returns:
            压缩统计
        """
        import time
        start_time = time.monotonic()

        logger.info("=== 记忆压缩流水线启动 ===")

        # Step 0: 收集记忆
        await self._collect_memories()
        original_count = len(self._memories)

        if original_count == 0:
            logger.info("没有可压缩的记忆，跳过")
            return CompressionStats()

        # Step 1: 聚类
        self._clusters = self._cluster_memories()
        logger.info(f"阶段 1/5 完成: 聚类 {len(self._clusters)} 个")

        # Step 2: 模式识别
        self._patterns = self._recognize_patterns()
        logger.info(f"阶段 2/5 完成: 识别模式 {len(self._patterns)} 个")

        # Step 3: 规则提取
        self._rules = self._extract_rules()
        logger.info(f"阶段 3/5 完成: 提取规则 {len(self._rules)} 条")

        # Step 4: T-Box 注入
        injected = self._inject_tbox()
        logger.info(f"阶段 4/5 完成: 注入 T-Box {injected} 条")

        # Step 5: 冷存储归档
        archived = await self._archive_cold_storage()
        logger.info(f"阶段 5/5 完成: 归档 {archived} 条")

        elapsed = time.monotonic() - start_time

        # 创建压缩后的汇总记忆
        compressed_count = await self._create_compressed_memories()

        stats = CompressionStats(
            original_count=original_count,
            compressed_count=compressed_count,
            rules_extracted=len(self._rules),
            clusters_formed=len(self._clusters),
            cold_archived=archived,
            compression_ratio=(
                compressed_count / original_count if original_count > 0 else 0.0
            ),
            duration_seconds=elapsed,
        )

        logger.info(f"=== 记忆压缩流水线完成: {stats.summary} ===")
        return stats

    # ------------------------------------------------------------------
    # 阶段 0: 收集记忆
    # ------------------------------------------------------------------

    async def _collect_memories(self) -> None:
        """从 MemoryManager 收集记忆"""
        self._memories = []

        # 搜索长期和语义记忆
        for mem_type in [MemoryType.LONG_TERM, MemoryType.SEMANTIC, MemoryType.EPISODIC]:
            results = await self._manager.search(
                self._config.search_query,
                memory_types=[mem_type],
                max_results=self._config.search_batch_size,
                similarity_threshold=0.0,  # 收集全部
            )
            for result in results:
                if result.memory.status not in (
                    MemoryStatus.ARCHIVED,
                    MemoryStatus.FORGOTTEN,
                ):
                    self._memories.append(result.memory)

        # 去重
        seen_ids: set[str] = set()
        unique: list[MemoryItem] = []
        for mem in self._memories:
            if mem.memory_id not in seen_ids:
                seen_ids.add(mem.memory_id)
                unique.append(mem)
        self._memories = unique

        logger.info(f"收集到 {len(self._memories)} 条可压缩记忆")

    # ------------------------------------------------------------------
    # 阶段 1: 聚类
    # ------------------------------------------------------------------

    def _cluster_memories(self) -> list[MemoryCluster]:
        """将记忆按向量相似度聚类

        优先使用 k-means（需要 numpy），否则回退到基于频率的分组。
        """
        # 尝试基于向量的聚类
        embeddings = [m.embedding for m in self._memories if m.embedding]
        if len(embeddings) >= self._config.min_cluster_size:
            try:
                return self._kmeans_cluster()
            except ImportError:
                logger.warning("numpy 不可用，回退到基于频率的聚类")
            except Exception as e:
                logger.warning(f"k-means 聚类失败 ({e})，回退到基于频率的聚类")

        # 回退方案：基于标签和内容关键词的频率分组
        return self._frequency_cluster()

    def _kmeans_cluster(self) -> list[MemoryCluster]:
        """基于 k-means 的向量聚类"""
        import numpy as np

        # 过滤有 embedding 的记忆
        with_embedding = [m for m in self._memories if m.embedding]
        if not with_embedding:
            return []

        vectors = np.array([m.embedding for m in with_embedding], dtype=np.float32)
        n_samples = len(vectors)

        # 自动确定聚类数
        k = self._config.n_clusters
        if k <= 0:
            k = min(
                max(2, int(math.sqrt(n_samples / 2))),
                self._config.max_clusters,
                n_samples // self._config.min_cluster_size,
            )
        k = max(2, min(k, n_samples))

        # 简易 k-means 实现
        dim = vectors.shape[1]
        indices = np.random.choice(n_samples, k, replace=False)
        centroids = vectors[indices].copy()

        max_iter = 30
        labels = np.zeros(n_samples, dtype=int)

        for _ in range(max_iter):
            # 分配：每个点到最近的质心
            # dists shape: (n_samples, k)
            dists = np.linalg.norm(
                vectors[:, np.newaxis, :] - centroids[np.newaxis, :, :], axis=2
            )
            new_labels = np.argmin(dists, axis=1)

            # 收敛检查
            if np.array_equal(new_labels, labels):
                break
            labels = new_labels

            # 更新质心
            for i in range(k):
                members = vectors[labels == i]
                if len(members) > 0:
                    centroids[i] = members.mean(axis=0)

        # 构建聚类对象
        clusters: list[MemoryCluster] = []
        for i in range(k):
            member_indices = np.where(labels == i)[0]
            if len(member_indices) < self._config.min_cluster_size:
                continue

            members = [with_embedding[int(idx)] for idx in member_indices]
            cluster = MemoryCluster(
                centroid=centroids[i].tolist(),
                members=members,
                avg_importance=(
                    sum(m.importance for m in members) / len(members)
                ),
            )
            clusters.append(cluster)

        logger.info(f"k-means 聚类: k={k}, 有效聚类={len(clusters)}")
        return clusters

    def _frequency_cluster(self) -> list[MemoryCluster]:
        """基于标签和关键词频率的聚类（无需 numpy）"""
        # 按标签分组
        tag_buckets: dict[str, list[MemoryItem]] = {}
        for memory in self._memories:
            if memory.tags:
                for tag in memory.tags:
                    tag_buckets.setdefault(tag, []).append(memory)

        # 按关键词分组
        keyword_buckets: dict[str, list[MemoryItem]] = {}
        for memory in self._memories:
            keywords = self._extract_keywords(memory.content)
            for kw in keywords:
                keyword_buckets.setdefault(kw, []).append(memory)

        # 合并：按频率最高的标签/关键词形成聚类
        all_buckets: dict[str, list[MemoryItem]] = {}
        all_buckets.update(tag_buckets)
        all_buckets.update(keyword_buckets)

        # 排序：优先大的桶
        sorted_buckets = sorted(
            all_buckets.items(), key=lambda kv: len(kv[1]), reverse=True
        )

        assigned: set[str] = set()  # memory_id
        clusters: list[MemoryCluster] = []

        for theme, candidates in sorted_buckets:
            members = [m for m in candidates if m.memory_id not in assigned]
            if len(members) < self._config.min_cluster_size:
                continue

            for m in members:
                assigned.add(m.memory_id)

            cluster = MemoryCluster(
                members=members,
                theme=theme,
                common_tags=self._most_common_tags(members),
                avg_importance=(
                    sum(m.importance for m in members) / len(members)
                ),
            )
            clusters.append(cluster)

            if len(clusters) >= self._config.max_clusters:
                break

        # 未分配的记忆归入一个杂项聚类
        orphans = [m for m in self._memories if m.memory_id not in assigned]
        if len(orphans) >= self._config.min_cluster_size:
            clusters.append(MemoryCluster(
                members=orphans,
                theme="miscellaneous",
                avg_importance=(
                    sum(m.importance for m in orphans) / len(orphans)
                ),
            ))

        logger.info(f"频率聚类: {len(clusters)} 个聚类")
        return clusters

    # ------------------------------------------------------------------
    # 阶段 2: 模式识别
    # ------------------------------------------------------------------

    def _recognize_patterns(self) -> list[dict[str, Any]]:
        """在聚类中识别共同主题和模式"""
        patterns: list[dict[str, Any]] = []

        for cluster in self._clusters:
            if cluster.size == 0:
                continue

            # 如果已经有主题（频率聚类赋值），直接使用
            if cluster.theme:
                theme = cluster.theme
            else:
                theme = self._identify_theme(cluster)

            # 提取共同标签
            common_tags = self._most_common_tags(cluster.members)

            # 提取共同关键词
            all_keywords: list[str] = []
            for mem in cluster.members:
                all_keywords.extend(self._extract_keywords(mem.content))
            keyword_freq = Counter(all_keywords)
            top_keywords = [
                kw for kw, cnt in keyword_freq.most_common(10)
                if cnt >= self._config.min_pattern_frequency
            ]

            # 内容类型分布
            type_dist = Counter(m.memory_type.value for m in cluster.members)

            # 重要性统计
            importances = [m.importance for m in cluster.members]

            pattern = {
                "cluster_id": cluster.cluster_id,
                "theme": theme,
                "common_tags": common_tags,
                "top_keywords": top_keywords,
                "type_distribution": dict(type_dist),
                "avg_importance": sum(importances) / len(importances),
                "max_importance": max(importances),
                "member_count": cluster.size,
                "example_contents": [
                    m.content[:200] for m in cluster.members[:3]
                ],
            }
            patterns.append(pattern)

            # 更新聚类的主题
            cluster.theme = theme
            cluster.common_tags = common_tags

        logger.info(f"模式识别: {len(patterns)} 个模式")
        return patterns

    def _identify_theme(self, cluster: MemoryCluster) -> str:
        """识别聚类主题"""
        # 从内容中提取高频名词/关键词作为主题
        all_keywords: list[str] = []
        for mem in cluster.members:
            all_keywords.extend(self._extract_keywords(mem.content))

        if not all_keywords:
            return "unnamed_cluster"

        freq = Counter(all_keywords)
        top = [kw for kw, _ in freq.most_common(3)]
        return "_".join(top)

    # ------------------------------------------------------------------
    # 阶段 3: 规则提取
    # ------------------------------------------------------------------

    def _extract_rules(self) -> list[ExtractedRule]:
        """将模式转化为结构化规则"""
        rules: list[ExtractedRule] = []

        for pattern in self._patterns:
            if pattern["member_count"] < self._config.min_cluster_size:
                continue

            theme = pattern["theme"]
            if theme == "miscellaneous":
                continue  # 杂项聚类不提取规则

            cluster_rules: list[ExtractedRule] = []

            # 规则 1: 概念规则 - 高频主题可以成为一个本体概念
            if pattern["avg_importance"] >= self._config.min_cluster_confidence:
                concept_rule = ExtractedRule(
                    name=f"concept:{theme}",
                    description=(
                        f"从 {pattern['member_count']} 条记忆中提取的概念: {theme}"
                    ),
                    source_cluster_ids=[pattern["cluster_id"]],
                    concept_name=theme,
                    properties={
                        "keywords": pattern["top_keywords"][:5],
                        "tags": pattern["common_tags"],
                        "type_distribution": pattern["type_distribution"],
                    },
                    confidence=min(
                        pattern["avg_importance"] + 0.1, 1.0
                    ),
                    example_contents=pattern["example_contents"],
                )
                cluster_rules.append(concept_rule)

            # 规则 2: 关系规则 - 共现标签之间可能存在关系
            tags = pattern["common_tags"]
            if len(tags) >= 2:
                for i in range(len(tags)):
                    for j in range(i + 1, min(len(tags), i + 3)):
                        rel_rule = ExtractedRule(
                            name=f"relation:{tags[i]}_related_to_{tags[j]}",
                            description=(
                                f"标签 '{tags[i]}' 和 '{tags[j]}' 在聚类 "
                                f"'{theme}' 中频繁共现"
                            ),
                            source_cluster_ids=[pattern["cluster_id"]],
                            relation_name=f"{tags[i]}_related_to_{tags[j]}",
                            properties={
                                "source_tag": tags[i],
                                "target_tag": tags[j],
                                "co_occurrence": pattern["member_count"],
                            },
                            confidence=pattern["avg_importance"],
                        )
                        cluster_rules.append(rel_rule)

            # 规则 3: 属性规则 - 从内容模式提取属性
            if pattern["top_keywords"]:
                prop_rule = ExtractedRule(
                    name=f"property:has_{theme}_attribute",
                    description=f"概念 '{theme}' 的属性: {', '.join(pattern['top_keywords'][:5])}",
                    source_cluster_ids=[pattern["cluster_id"]],
                    concept_name=theme,
                    properties={
                        "attribute_keywords": pattern["top_keywords"][:5],
                    },
                    confidence=pattern["avg_importance"],
                )
                cluster_rules.append(prop_rule)

            # 限制每个聚类的规则数
            cluster_rules.sort(key=lambda r: r.confidence, reverse=True)
            rules.extend(
                cluster_rules[: self._config.max_rules_per_cluster]
            )

        logger.info(f"规则提取: {len(rules)} 条规则")
        return rules

    # ------------------------------------------------------------------
    # 阶段 4: T-Box 注入
    # ------------------------------------------------------------------

    def _inject_tbox(self) -> int:
        """将提取的规则注入本体引擎 T-Box

        Returns:
            注入的规则数
        """
        injected = 0
        seen_concepts: set[str] = set()
        seen_relations: set[str] = set()

        for rule in self._rules:
            # 注入概念
            if rule.concept_name and rule.concept_name not in seen_concepts:
                # 检查是否已存在
                existing = self._ontology._name_to_concept.get(
                    rule.concept_name.lower()
                )
                if not existing:
                    concept = Concept(
                        name=rule.concept_name,
                        description=rule.description,
                        properties=[],
                        metadata={
                            "source": "memory_compression",
                            "confidence": rule.confidence,
                            "keywords": rule.properties.get("keywords", []),
                            "tags": rule.properties.get("tags", []),
                        },
                    )
                    self._ontology.add_concept(concept)
                    injected += 1
                seen_concepts.add(rule.concept_name)

            # 注入属性定义
            attr_keywords = rule.properties.get("attribute_keywords", [])
            if rule.concept_name and attr_keywords:
                for kw in attr_keywords[:3]:
                    prop = PropertyDefinition(
                        name=f"has_{kw}",
                        domain=rule.concept_name,
                        range_type="string",
                        metadata={
                            "source": "memory_compression",
                            "confidence": rule.confidence,
                        },
                    )
                    self._ontology.add_property_definition(prop)
                    injected += 1

            # 注入关系
            if rule.relation_name and rule.relation_name not in seen_relations:
                existing = None
                for rd in self._ontology._relation_defs.values():
                    if rd.name == rule.relation_name:
                        existing = rd
                        break

                if not existing:
                    source_tag = rule.properties.get("source_tag", "")
                    target_tag = rule.properties.get("target_tag", "")
                    rel_def = RelationDefinition(
                        name=rule.relation_name,
                        relation_type=RelationType.RELATED_TO,
                        domain_concept=source_tag,
                        range_concept=target_tag,
                        metadata={
                            "source": "memory_compression",
                            "confidence": rule.confidence,
                        },
                    )
                    self._ontology.add_relation_definition(rel_def)
                    injected += 1
                seen_relations.add(rule.relation_name)

        logger.info(f"T-Box 注入: {injected} 条")
        return injected

    # ------------------------------------------------------------------
    # 阶段 5: 冷存储归档
    # ------------------------------------------------------------------

    async def _archive_cold_storage(self) -> int:
        """将原始低重要性记忆标记为归档

        Returns:
            归档的记忆数
        """
        archived = 0
        threshold = self._config.archive_importance_threshold

        for memory in self._memories:
            if memory.importance < threshold:
                memory.status = MemoryStatus.ARCHIVED
                archived += 1

        logger.info(f"冷存储归档: {archived} 条记忆")
        return archived

    # ------------------------------------------------------------------
    # 压缩记忆创建
    # ------------------------------------------------------------------

    async def _create_compressed_memories(self) -> int:
        """为每个聚类创建一条压缩汇总记忆

        Returns:
            创建的压缩记忆数
        """
        created = 0

        for cluster in self._clusters:
            if cluster.size == 0:
                continue

            # 构建汇总内容
            summary = self._build_cluster_summary(cluster)

            # 计算聚合重要性：取聚类内最高的重要性
            max_importance = max(m.importance for m in cluster.members)

            # 合并标签
            all_tags: list[str] = []
            for m in cluster.members:
                all_tags.extend(m.tags)
            tag_freq = Counter(all_tags)
            merged_tags = [
                "compressed",
                f"cluster_{cluster.cluster_id[:8]}",
            ] + [t for t, _ in tag_freq.most_common(5)]

            try:
                await self._manager.add_memory(
                    content=summary,
                    memory_type=self._config.cold_storage_memory_type,
                    priority=MemoryPriority.HIGH,
                    tags=merged_tags,
                    importance=min(max_importance + 0.1, 1.0),
                    metadata={
                        "source": "memory_compression",
                        "cluster_id": cluster.cluster_id,
                        "original_count": cluster.size,
                        "theme": cluster.theme,
                    },
                )
                created += 1
            except Exception as e:
                logger.error(f"创建压缩记忆失败: {e}")

        logger.info(f"创建压缩记忆: {created} 条")
        return created

    def _build_cluster_summary(self, cluster: MemoryCluster) -> str:
        """构建聚类的文字摘要"""
        parts: list[str] = []

        parts.append(f"[压缩记忆] 主题: {cluster.theme}")
        parts.append(f"来源: {cluster.size} 条原始记忆")
        parts.append(f"平均重要性: {cluster.avg_importance:.2f}")

        if cluster.common_tags:
            parts.append(f"共同标签: {', '.join(cluster.common_tags[:5])}")

        # 选取最具代表性的内容（重要性最高的几条）
        sorted_members = sorted(
            cluster.members, key=lambda m: m.importance, reverse=True
        )
        examples = sorted_members[:3]
        parts.append("代表性内容:")
        for i, mem in enumerate(examples, 1):
            content_preview = mem.content[:150].replace("\n", " ")
            parts.append(f"  {i}. [{mem.importance:.2f}] {content_preview}")

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_keywords(text: str) -> list[str]:
        """从文本中提取关键词

        使用简单的分词策略：按标点和空格分割，过滤停用词和短词。
        """
        # 简单的中英文分词
        # 英文：按空格和标点分割
        # 中文：按字符 bigram
        words: list[str] = []

        # 英文单词
        english_words = re.findall(r"[a-zA-Z]{2,}", text)
        words.extend(w.lower() for w in english_words)

        # 中文字符 bigram（简单近似）
        chinese_chars = re.findall(r"[一-鿿]+", text)
        for segment in chinese_chars:
            if len(segment) >= 2:
                for i in range(len(segment) - 1):
                    words.append(segment[i : i + 2])
            if len(segment) >= 1:
                words.append(segment)

        # 过滤停用词
        stop_words = {
            "the", "is", "at", "which", "on", "a", "an", "and", "or", "but",
            "in", "with", "to", "for", "of", "not", "no", "can", "had", "has",
            "this", "that", "it", "be", "as", "was", "were", "been", "are",
            "from", "by", "do", "did", "will", "would", "should", "could",
            "的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都",
            "一", "一个", "上", "也", "很", "到", "说", "要", "去", "你",
            "会", "着", "没有", "看", "好", "自己", "这",
        }

        return [w for w in words if w not in stop_words and len(w) >= 2]

    @staticmethod
    def _most_common_tags(members: list[MemoryItem], top_n: int = 5) -> list[str]:
        """提取最常见的标签"""
        tag_counter: Counter[str] = Counter()
        for mem in members:
            tag_counter.update(mem.tags)
        return [tag for tag, _ in tag_counter.most_common(top_n)]

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """计算余弦相似度（复用 MemoryManager 的逻辑）"""
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)
