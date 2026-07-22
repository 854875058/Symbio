"""Dataset Exporter — 自动清洗、脱敏、质量过滤并格式化为标准微调数据集。

支持格式：ShareGPT / Alpaca / OpenAI
核心能力：PII 脱敏、质量过滤、去重去噪、增量导出
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger
from symbio.utils.types import Trajectory

logger = get_logger("dataset_exporter")


# =============================================================================
# 1. 导出格式枚举与配置模型
# =============================================================================


class ExportFormat(str, Enum):
    """支持的微调数据集格式。"""

    SHAREGPT = "sharegpt"
    ALPACA = "alpaca"
    OPENAI = "openai"


class PIIMaskConfig(BaseModel):
    """PII 脱敏配置。"""

    mask_email: bool = Field(default=True, description="脱敏邮箱地址")
    mask_phone: bool = Field(default=True, description="脱敏手机号")
    mask_id_card: bool = Field(default=True, description="脱敏身份证号")
    mask_bank_card: bool = Field(default=True, description="脱敏银行卡号")
    mask_api_key: bool = Field(default=True, description="脱敏 API 密钥")
    mask_ip_address: bool = Field(default=False, description="脱敏 IP 地址")
    mask_url_credentials: bool = Field(default=True, description="脱敏 URL 中的凭证")
    custom_patterns: dict[str, str] = Field(
        default_factory=dict,
        description="自定义脱敏规则，格式: {pattern_name: regex_pattern}",
    )
    replacement_template: str = Field(
        default="[{category}]",
        description="替换模板，{category} 会被替换为类别名",
    )


class QualityFilterConfig(BaseModel):
    """质量过滤配置。"""

    min_content_length: int = Field(default=10, description="最小内容长度（字符）")
    max_content_length: int = Field(default=32000, description="最大内容长度（字符）")
    min_turns: int = Field(default=1, description="最少对话轮次")
    max_turns: int = Field(default=100, description="最多对话轮次")
    min_quality_score: float = Field(default=0.3, description="最低质量评分 (0-1)")
    max_duplicate_similarity: float = Field(default=0.95, description="去重相似度阈值 (0-1)")
    remove_empty_messages: bool = Field(default=True, description="移除空消息")
    remove_code_only_messages: bool = Field(default=False, description="移除纯代码消息")
    min_instruction_length: int = Field(default=5, description="指令最小长度（仅 Alpaca 格式）")


class ExportConfig(BaseModel):
    """导出器总配置。"""

    output_dir: str = Field(default="./data/datasets", description="输出目录")
    format: ExportFormat = Field(default=ExportFormat.SHAREGPT, description="导出格式")
    pii_mask: PIIMaskConfig = Field(default_factory=PIIMaskConfig)
    quality_filter: QualityFilterConfig = Field(default_factory=QualityFilterConfig)
    incremental: bool = Field(default=True, description="是否增量导出")
    dedup_by_hash: bool = Field(default=True, description="是否按内容哈希去重")
    max_concurrent: int = Field(default=10, description="最大并发处理数")
    batch_size: int = Field(default=100, description="批量处理大小")
    file_prefix: str = Field(default="symbio_dataset", description="输出文件前缀")


# =============================================================================
# 2. 导出格式数据模型
# =============================================================================


class ShareGPTMessage(BaseModel):
    """ShareGPT 格式单条消息。"""

    role: str = Field(description="角色: human / gpt / system / tool")
    content: str = Field(description="消息内容")


class ShareGPTSample(BaseModel):
    """ShareGPT 格式样本。"""

    id: str = Field(default_factory=lambda: str(uuid4()))
    conversations: list[ShareGPTMessage] = Field(description="对话列表")


class AlpacaSample(BaseModel):
    """Alpaca 格式样本。"""

    id: str = Field(default_factory=lambda: str(uuid4()))
    instruction: str = Field(description="指令")
    input: str = Field(default="", description="输入上下文")
    output: str = Field(description="输出")


class OpenAIMessage(BaseModel):
    """OpenAI Chat 格式单条消息。"""

    role: str = Field(description="角色: system / user / assistant / tool")
    content: str = Field(description="消息内容")


class OpenAISample(BaseModel):
    """OpenAI Chat 格式样本。"""

    id: str = Field(default_factory=lambda: str(uuid4()))
    messages: list[OpenAIMessage] = Field(description="消息列表")


# =============================================================================
# 3. PII 脱敏引擎
# =============================================================================


class PIIDetector:
    """PII（个人可识别信息）检测与脱敏器。

    支持检测：邮箱、手机号、身份证号、银行卡号、API 密钥、IP 地址、URL 凭证。
    支持自定义正则规则扩展。
    """

    # 预定义正则模式
    PATTERNS: dict[str, re.Pattern] = {
        "email": re.compile(
            r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
            re.IGNORECASE,
        ),
        "phone_cn": re.compile(
            r"(?<!\d)"
            r"(?:\+?86[\-\s]?)?"
            r"1[3-9]\d{9}"
            r"(?!\d)"
        ),
        "phone_intl": re.compile(
            r"(?<!\d)"
            r"\+?[1-9]\d{1,3}[\-\s.]?\(?\d{1,4}\)?[\-\s.]?\d{1,4}[\-\s.]?\d{1,9}"
            r"(?!\d)"
        ),
        "id_card_cn": re.compile(
            r"(?<!\d)"
            r"[1-9]\d{5}"
            r"(?:19|20)\d{2}"
            r"(?:0[1-9]|1[0-2])"
            r"(?:0[1-9]|[12]\d|3[01])"
            r"\d{3}[\dXx]"
            r"(?!\d)"
        ),
        "bank_card": re.compile(
            r"(?<!\d)"
            r"(?:6[0-9]{15,18}|4[0-9]{15}|5[1-5][0-9]{14}|3[47][0-9]{13})"
            r"(?!\d)"
        ),
        "api_key": re.compile(
            r"(?i)"
            r"(?:sk[\-_]|api[\-_]?key[\s:=]+|bearer\s+|token[\s:=]+)"
            r"[a-zA-Z0-9\-_]{20,}"
        ),
        "aws_key": re.compile(r"AKIA[0-9A-Z]{16}"),
        "github_token": re.compile(r"gh[pousr]_[A-Za-z0-9_]{36,}"),
        "private_key": re.compile(
            r"-----BEGIN\s+(?:RSA|EC|DSA|OPENSSH)?\s*PRIVATE KEY-----",
            re.IGNORECASE,
        ),
        "ip_address": re.compile(
            r"(?<!\d)"
            r"(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}"
            r"(?:25[0-5]|2[0-4]\d|[01]?\d\d?)"
            r"(?!\d)"
        ),
        "url_credentials": re.compile(
            r"(https?://)[^:]+:[^@]+@",
            re.IGNORECASE,
        ),
    }

    def __init__(self, config: PIIMaskConfig) -> None:
        self._config = config
        self._active_patterns: dict[str, re.Pattern] = {}
        self._build_active_patterns()

    def _build_active_patterns(self) -> None:
        """根据配置构建启用的正则模式集合。"""
        cfg = self._config

        toggle_map = {
            "email": cfg.mask_email,
            "phone_cn": cfg.mask_phone,
            "phone_intl": cfg.mask_phone,
            "id_card_cn": cfg.mask_id_card,
            "bank_card": cfg.mask_bank_card,
            "api_key": cfg.mask_api_key,
            "aws_key": cfg.mask_api_key,
            "github_token": cfg.mask_api_key,
            "private_key": cfg.mask_api_key,
            "ip_address": cfg.mask_ip_address,
            "url_credentials": cfg.mask_url_credentials,
        }

        for name, enabled in toggle_map.items():
            if enabled and name in self.PATTERNS:
                self._active_patterns[name] = self.PATTERNS[name]

        # 添加自定义规则
        for pattern_name, pattern_str in cfg.custom_patterns.items():
            try:
                self._active_patterns[f"custom_{pattern_name}"] = re.compile(
                    pattern_str, re.IGNORECASE
                )
            except re.error as exc:
                logger.warning(f"Invalid custom PII pattern '{pattern_name}': {exc}, skipped")

    def detect(self, text: str) -> dict[str, list[str]]:
        """检测文本中的 PII 信息。

        Args:
            text: 待检测文本

        Returns:
            {category: [matched_values]}
        """
        findings: dict[str, list[str]] = {}
        for category, pattern in self._active_patterns.items():
            matches = pattern.findall(text)
            if matches:
                findings[category] = matches
        return findings

    def mask(self, text: str) -> str:
        """对文本中的 PII 信息进行脱敏替换。

        Args:
            text: 原始文本

        Returns:
            脱敏后的文本
        """
        result = text
        for category, pattern in self._active_patterns.items():
            display_name = category.replace("_", " ").title()
            replacement = self._config.replacement_template.format(category=display_name)
            result = pattern.sub(replacement, result)
        return result

    def mask_messages(self, messages: list[dict[str, str]]) -> list[dict[str, str]]:
        """对消息列表批量脱敏。

        Args:
            messages: [{"role": ..., "content": ...}, ...]

        Returns:
            脱敏后的消息列表
        """
        return [{"role": m["role"], "content": self.mask(m["content"])} for m in messages]


# =============================================================================
# 4. 质量过滤器
# =============================================================================


class QualityScorer:
    """轨迹质量评分器。

    基于多维度指标对轨迹进行质量评分：
    - 内容长度合理性
    - 对话轮次完整性
    - 消息有效性（非空、非噪音）
    - 工具调用丰富度
    - 成功完成度
    """

    def __init__(self, config: QualityFilterConfig) -> None:
        self._config = config

    def score(self, trajectory: Trajectory) -> float:
        """计算轨迹的综合质量评分 (0-1)。

        Args:
            trajectory: 执行轨迹

        Returns:
            质量评分，越高越好
        """
        scores: list[float] = []
        weights: list[float] = []

        # 1. 内容长度评分
        length_score = self._score_content_length(trajectory)
        scores.append(length_score)
        weights.append(0.2)

        # 2. 轮次完整性评分
        turn_score = self._score_turns(trajectory)
        scores.append(turn_score)
        weights.append(0.15)

        # 3. 消息有效性评分
        validity_score = self._score_message_validity(trajectory)
        scores.append(validity_score)
        weights.append(0.25)

        # 4. 工具调用丰富度评分
        tool_score = self._score_tool_usage(trajectory)
        scores.append(tool_score)
        weights.append(0.15)

        # 5. 成功完成度评分
        success_score = self._score_success(trajectory)
        scores.append(success_score)
        weights.append(0.25)

        total_weight = sum(weights)
        if total_weight == 0:
            return 0.0

        weighted_sum = sum(s * w for s, w in zip(scores, weights))
        return round(weighted_sum / total_weight, 4)

    def _score_content_length(self, trajectory: Trajectory) -> float:
        """评估内容总长度是否在合理范围内。"""
        total_length = sum(
            len(step.thought) + len(step.action) + len(step.observation)
            for step in trajectory.steps
        )
        cfg = self._config

        if total_length < cfg.min_content_length:
            return 0.1
        if total_length > cfg.max_content_length:
            return 0.5

        # 理想范围: 100-8000 字符
        if 100 <= total_length <= 8000:
            return 1.0
        if total_length < 100:
            return total_length / 100 * 0.7 + 0.3
        # total_length > 8000
        span = cfg.max_content_length - 8000
        if span <= 0:
            return 0.5
        return max(0.5, 1.0 - (total_length - 8000) / span * 0.5)

    def _score_turns(self, trajectory: Trajectory) -> float:
        """评估对话轮次是否合理。"""
        step_count = len(trajectory.steps)
        cfg = self._config

        if step_count < cfg.min_turns:
            return 0.1
        if step_count > cfg.max_turns:
            return 0.3

        # 理想: 2-20 步
        if 2 <= step_count <= 20:
            return 1.0
        return 0.6

    def _score_message_validity(self, trajectory: Trajectory) -> float:
        """评估消息的有效性比例。"""
        total_fields = 0
        valid_fields = 0

        for step in trajectory.steps:
            for content in (step.thought, step.action, step.observation):
                total_fields += 1
                stripped = content.strip()
                if stripped and len(stripped) > 2:
                    # 检查是否是噪音（全重复字符、纯标点等）
                    if not self._is_noise(stripped):
                        valid_fields += 1

        if total_fields == 0:
            return 0.0
        return valid_fields / total_fields

    def _score_tool_usage(self, trajectory: Trajectory) -> float:
        """评估工具调用的丰富度。"""
        total_tool_calls = sum(len(step.tool_calls) for step in trajectory.steps)
        unique_tools = set()
        for step in trajectory.steps:
            for tc in step.tool_calls:
                unique_tools.add(tc.tool_name)

        # 有工具调用得基础分
        if total_tool_calls == 0:
            return 0.3

        # 工具多样性加分
        diversity_bonus = min(len(unique_tools) / 3, 1.0) * 0.4
        # 调用数量加分
        count_bonus = min(total_tool_calls / 5, 1.0) * 0.3

        return 0.3 + diversity_bonus + count_bonus

    def _score_success(self, trajectory: Trajectory) -> float:
        """评估任务是否成功完成。"""
        if trajectory.success:
            return 1.0

        # 有最终结果但不成功
        if trajectory.final_result is not None:
            return 0.4

        # 无最终结果
        return 0.2

    @staticmethod
    def _is_noise(text: str) -> bool:
        """检测文本是否为噪音内容。"""
        if not text:
            return True
        # 全重复字符
        if len(set(text.strip())) <= 2 and len(text) > 3:
            return True
        # 纯标点或纯空白
        cleaned = re.sub(r"[\s\W]", "", text)
        if len(cleaned) < 2:
            return True
        # 纯数字
        if text.strip().isdigit() and len(text.strip()) < 3:
            return True
        return False


class QualityFilter:
    """质量过滤器，根据多维度规则过滤低质量数据。"""

    def __init__(self, config: QualityFilterConfig) -> None:
        self._config = config
        self._scorer = QualityScorer(config)

    def filter_trajectory(self, trajectory: Trajectory) -> tuple[bool, float, list[str]]:
        """过滤单条轨迹。

        Args:
            trajectory: 执行轨迹

        Returns:
            (是否通过, 质量评分, 拒绝原因列表)
        """
        reasons: list[str] = []

        # 1. 基本有效性检查
        if not trajectory.steps:
            reasons.append("empty_trajectory")
            return False, 0.0, reasons

        # 2. 质量评分
        score = self._scorer.score(trajectory)
        if score < self._config.min_quality_score:
            reasons.append(f"low_quality_score:{score:.3f}")

        # 3. 内容长度检查
        total_length = sum(
            len(step.thought) + len(step.action) + len(step.observation)
            for step in trajectory.steps
        )
        if total_length < self._config.min_content_length:
            reasons.append(f"too_short:{total_length}")
        if total_length > self._config.max_content_length:
            reasons.append(f"too_long:{total_length}")

        # 4. 轮次检查
        step_count = len(trajectory.steps)
        if step_count < self._config.min_turns:
            reasons.append(f"too_few_turns:{step_count}")
        if step_count > self._config.max_turns:
            reasons.append(f"too_many_turns:{step_count}")

        # 5. 空消息检查
        if self._config.remove_empty_messages:
            has_content = any(
                step.thought.strip() or step.action.strip() or step.observation.strip()
                for step in trajectory.steps
            )
            if not has_content:
                reasons.append("all_messages_empty")

        passed = len(reasons) == 0
        return passed, score, reasons


# =============================================================================
# 5. 数据清洗管道
# =============================================================================


class DataCleaner:
    """数据清洗管道：去重、去噪、格式标准化。"""

    def __init__(self, config: ExportConfig) -> None:
        self._config = config
        self._seen_hashes: set[str] = set()
        self._load_existing_hashes()

    def _load_existing_hashes(self) -> None:
        """加载已存在的内容哈希，用于去重。"""
        hash_file = Path(self._config.output_dir) / ".content_hashes.json"
        if hash_file.exists():
            try:
                with open(hash_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._seen_hashes = set(data.get("hashes", []))
                logger.info(f"Loaded {len(self._seen_hashes)} existing content hashes for dedup")
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(f"Failed to load content hashes: {exc}")

    def save_hashes(self) -> None:
        """持久化内容哈希集合。"""
        hash_file = Path(self._config.output_dir) / ".content_hashes.json"
        hash_file.parent.mkdir(parents=True, exist_ok=True)
        with open(hash_file, "w", encoding="utf-8") as f:
            json.dump(
                {"hashes": list(self._seen_hashes), "updated_at": datetime.now().isoformat()},
                f,
                ensure_ascii=False,
            )

    @staticmethod
    def compute_content_hash(messages: list[dict[str, str]]) -> str:
        """计算消息列表的内容哈希。

        Args:
            messages: [{"role": ..., "content": ...}, ...]

        Returns:
            内容的 SHA-256 哈希值
        """
        canonical = json.dumps(
            [(m["role"], m["content"].strip()) for m in messages],
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def is_duplicate(self, messages: list[dict[str, str]]) -> bool:
        """检查消息列表是否重复。

        Args:
            messages: [{"role": ..., "content": ...}, ...]

        Returns:
            是否重复
        """
        if not self._config.dedup_by_hash:
            return False
        content_hash = self.compute_content_hash(messages)
        return content_hash in self._seen_hashes

    def mark_seen(self, messages: list[dict[str, str]]) -> None:
        """标记消息列表为已处理。"""
        if self._config.dedup_by_hash:
            content_hash = self.compute_content_hash(messages)
            self._seen_hashes.add(content_hash)

    @staticmethod
    def clean_text(text: str) -> str:
        """清洗单条文本内容。

        - 移除多余空白
        - 标准化换行符
        - 移除不可见控制字符（保留换行和制表）
        - 去除首尾空白
        """
        if not text:
            return ""

        # 标准化换行
        text = text.replace("\r\n", "\n").replace("\r", "\n")

        # 移除不可见控制字符（保留 \n \t）
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)

        # 移除连续空行（保留最多两个换行）
        text = re.sub(r"\n{3,}", "\n\n", text)

        # 移除行尾空白
        text = re.sub(r"[ \t]+\n", "\n", text)

        return text.strip()

    def clean_messages(
        self, messages: list[dict[str, str]], remove_empty: bool = True
    ) -> list[dict[str, str]]:
        """清洗消息列表。

        Args:
            messages: [{"role": ..., "content": ...}, ...]
            remove_empty: 是否移除清洗后为空的消息

        Returns:
            清洗后的消息列表
        """
        cleaned = []
        for msg in messages:
            content = self.clean_text(msg.get("content", ""))
            role = msg.get("role", "").strip().lower()

            if not role:
                continue

            if remove_empty and not content:
                continue

            cleaned.append({"role": role, "content": content})

        return cleaned

    def normalize_role(self, role: str, target_format: ExportFormat) -> str:
        """标准化角色名称以匹配目标格式。

        Args:
            role: 原始角色名
            target_format: 目标导出格式

        Returns:
            标准化后的角色名
        """
        role = role.strip().lower()

        # 统一中间表示
        role_map = {
            "human": "user",
            "user": "user",
            "gpt": "assistant",
            "assistant": "assistant",
            "ai": "assistant",
            "bot": "assistant",
            "system": "system",
            "tool": "tool",
            "function": "tool",
        }
        normalized = role_map.get(role, "user")

        # 按目标格式映射
        if target_format == ExportFormat.SHAREGPT:
            sharegpt_map = {
                "user": "human",
                "assistant": "gpt",
                "system": "system",
                "tool": "tool",
            }
            return sharegpt_map.get(normalized, "human")

        # Alpaca 和 OpenAI 使用标准名称
        return normalized


# =============================================================================
# 6. 增量导出追踪器
# =============================================================================


class ExportTracker:
    """增量导出追踪器，记录已导出的轨迹 ID。"""

    def __init__(self, output_dir: str) -> None:
        self._tracker_file = Path(output_dir) / ".export_tracker.json"
        self._exported_ids: set[str] = set()
        self._load()

    def _load(self) -> None:
        """加载已导出记录。"""
        if self._tracker_file.exists():
            try:
                with open(self._tracker_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._exported_ids = set(data.get("exported_ids", []))
                logger.info(
                    f"Loaded export tracker: {len(self._exported_ids)} previously exported trajectories"
                )
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(f"Failed to load export tracker: {exc}")

    def save(self) -> None:
        """持久化已导出记录。"""
        self._tracker_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self._tracker_file, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "exported_ids": list(self._exported_ids),
                    "updated_at": datetime.now().isoformat(),
                    "total_count": len(self._exported_ids),
                },
                f,
                ensure_ascii=False,
            )

    def is_exported(self, trajectory_id: str) -> bool:
        """检查轨迹是否已导出。"""
        return trajectory_id in self._exported_ids

    def mark_exported(self, trajectory_id: str) -> None:
        """标记轨迹为已导出。"""
        self._exported_ids.add(trajectory_id)

    def mark_batch(self, trajectory_ids: list[str]) -> None:
        """批量标记为已导出。"""
        self._exported_ids.update(trajectory_ids)

    @property
    def exported_count(self) -> int:
        """已导出总数。"""
        return len(self._exported_ids)


# =============================================================================
# 7. 格式转换器
# =============================================================================


class FormatConverter:
    """将轨迹转换为不同微调数据格式。"""

    def __init__(
        self,
        config: ExportConfig,
        cleaner: DataCleaner,
        pii_detector: PIIDetector,
    ) -> None:
        self._config = config
        self._cleaner = cleaner
        self._pii = pii_detector

    def convert(self, trajectory: Trajectory) -> dict[str, Any] | None:
        """将轨迹转换为目标格式的样本。

        Args:
            trajectory: 执行轨迹

        Returns:
            转换后的样本字典，失败返回 None
        """
        messages = self._trajectory_to_messages(trajectory)
        if not messages:
            return None

        # 清洗
        messages = self._cleaner.clean_messages(
            messages, remove_empty=self._config.quality_filter.remove_empty_messages
        )
        if not messages:
            return None

        # PII 脱敏
        messages = self._pii.mask_messages(messages)

        # 角色标准化
        for msg in messages:
            msg["role"] = self._cleaner.normalize_role(msg["role"], self._config.format)

        # 按目标格式转换
        fmt = self._config.format
        if fmt == ExportFormat.SHAREGPT:
            return self._to_sharegpt(trajectory, messages)
        if fmt == ExportFormat.ALPACA:
            return self._to_alpaca(trajectory, messages)
        if fmt == ExportFormat.OPENAI:
            return self._to_openai(trajectory, messages)

        logger.error(f"Unsupported export format: {fmt}")
        return None

    def _trajectory_to_messages(self, trajectory: Trajectory) -> list[dict[str, str]]:
        """将轨迹转换为消息列表。"""
        messages: list[dict[str, str]] = []

        # 系统提示（如果有）
        if trajectory.steps:
            first_step = trajectory.steps[0]
            if first_step.thought and first_step.thought.startswith("System:"):
                messages.append(
                    {"role": "system", "content": first_step.thought[len("System:") :].strip()}
                )

        for step in trajectory.steps:
            # Thought 作为 assistant 的推理过程
            if step.thought and not step.thought.startswith("System:"):
                messages.append({"role": "assistant", "content": step.thought})

            # Action 作为 assistant 的操作
            if step.action:
                action_content = step.action
                # 附加工具调用信息
                if step.tool_calls:
                    tool_info = []
                    for tc in step.tool_calls:
                        params_str = json.dumps(tc.parameters, ensure_ascii=False)
                        tool_info.append(f"Tool: {tc.tool_name}({params_str})")
                    action_content += "\n" + "\n".join(tool_info)
                messages.append({"role": "assistant", "content": action_content})

            # Tool results 作为 tool 消息
            for tr in step.tool_results:
                tool_content = tr.output
                if tr.error:
                    tool_content = f"Error: {tr.error}\n{tool_content}"
                messages.append({"role": "tool", "content": tool_content})

            # Observation 作为 user 观察反馈
            if step.observation:
                messages.append({"role": "user", "content": step.observation})

        # 附加最终结果
        if trajectory.final_result and trajectory.final_result.content:
            messages.append({"role": "assistant", "content": trajectory.final_result.content})

        return messages

    def _to_sharegpt(
        self, trajectory: Trajectory, messages: list[dict[str, str]]
    ) -> dict[str, Any]:
        """转换为 ShareGPT 格式。"""
        # 合并连续同角色消息
        merged = self._merge_consecutive(messages)
        conversations = [ShareGPTMessage(role=m["role"], content=m["content"]) for m in merged]
        sample = ShareGPTSample(
            id=trajectory.trajectory_id,
            conversations=conversations,
        )
        return sample.model_dump()

    def _to_alpaca(
        self, trajectory: Trajectory, messages: list[dict[str, str]]
    ) -> dict[str, Any] | None:
        """转换为 Alpaca 格式。

        提取第一个 user 消息作为 instruction，中间对话作为 input，
        最后一个 assistant 消息作为 output。
        """
        user_msgs = [m for m in messages if m["role"] == "user"]
        assistant_msgs = [m for m in messages if m["role"] == "assistant"]

        if not user_msgs or not assistant_msgs:
            return None

        instruction = user_msgs[0]["content"]
        if len(instruction) < self._config.quality_filter.min_instruction_length:
            return None

        # 中间的 user 消息拼接为 input
        input_text = "\n\n".join(m["content"] for m in user_msgs[1:]) if len(user_msgs) > 1 else ""

        # 最后一个 assistant 消息作为 output
        output = assistant_msgs[-1]["content"]

        sample = AlpacaSample(
            id=trajectory.trajectory_id,
            instruction=instruction,
            input=input_text,
            output=output,
        )
        return sample.model_dump()

    def _to_openai(self, trajectory: Trajectory, messages: list[dict[str, str]]) -> dict[str, Any]:
        """转换为 OpenAI Chat 格式。"""
        merged = self._merge_consecutive(messages)
        openai_messages = [OpenAIMessage(role=m["role"], content=m["content"]) for m in merged]
        sample = OpenAISample(
            id=trajectory.trajectory_id,
            messages=openai_messages,
        )
        return sample.model_dump()

    @staticmethod
    def _merge_consecutive(
        messages: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        """合并连续相同角色的消息。"""
        if not messages:
            return []

        merged = [messages[0].copy()]
        for msg in messages[1:]:
            if msg["role"] == merged[-1]["role"]:
                merged[-1]["content"] += "\n\n" + msg["content"]
            else:
                merged.append(msg.copy())
        return merged


# =============================================================================
# 8. 数据集导出器主类
# =============================================================================


class DatasetExporter:
    """数据集导出器 — 从执行轨迹自动生产高质量微调数据集。

    核心能力:
    - 多格式导出: ShareGPT / Alpaca / OpenAI
    - 自动清洗: 去重、去噪、格式标准化
    - 质量过滤: 基于长度、完整性、语义质量评分
    - PII 脱敏: 自动检测并移除邮箱、手机号、密钥等敏感信息
    - 增量导出: 只导出新增的高质量数据

    Usage::

        config = ExportConfig(format=ExportFormat.SHAREGPT)
        exporter = DatasetExporter(config)

        # 同步导出
        report = exporter.export(trajectories)

        # 异步导出
        report = await exporter.export_async(trajectories)
    """

    def __init__(self, config: ExportConfig | None = None) -> None:
        self._config = config or ExportConfig()
        self._pii_detector = PIIDetector(self._config.pii_mask)
        self._quality_filter = QualityFilter(self._config.quality_filter)
        self._cleaner = DataCleaner(self._config)
        self._tracker = ExportTracker(self._config.output_dir)
        self._converter = FormatConverter(self._config, self._cleaner, self._pii_detector)

    @property
    def config(self) -> ExportConfig:
        """当前导出配置。"""
        return self._config

    @property
    def tracker(self) -> ExportTracker:
        """增量导出追踪器。"""
        return self._tracker

    def export(self, trajectories: list[Trajectory]) -> ExportReport:
        """同步导出数据集。

        Args:
            trajectories: 待处理的轨迹列表

        Returns:
            导出报告
        """
        report = ExportReport(
            format=self._config.format,
            started_at=datetime.now(),
        )

        # 确保输出目录存在
        output_dir = Path(self._config.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # 增量过滤
        candidates = trajectories
        if self._config.incremental:
            candidates = [t for t in trajectories if not self._tracker.is_exported(t.trajectory_id)]
            report.skipped_incremental = len(trajectories) - len(candidates)
            logger.info(
                f"Incremental filter: {len(candidates)} new trajectories "
                f"({report.skipped_incremental} already exported)"
            )

        samples: list[dict[str, Any]] = []

        for trajectory in candidates:
            report.total_processed += 1

            # 质量过滤
            passed, score, reasons = self._quality_filter.filter_trajectory(trajectory)
            if not passed:
                report.rejected += 1
                for reason in reasons:
                    category = reason.split(":")[0]
                    report.rejection_reasons[category] = (
                        report.rejection_reasons.get(category, 0) + 1
                    )
                logger.debug(f"Trajectory {trajectory.trajectory_id} rejected: {reasons}")
                continue

            report.quality_scores.append(score)

            # 构建消息列表用于去重检查
            messages = self._converter._trajectory_to_messages(trajectory)
            if messages:
                messages = self._cleaner.clean_messages(messages)

            # 去重检查
            if messages and self._cleaner.is_duplicate(messages):
                report.duplicates += 1
                logger.debug(f"Trajectory {trajectory.trajectory_id} is duplicate, skipped")
                continue

            # 格式转换
            sample = self._converter.convert(trajectory)
            if sample is None:
                report.rejected += 1
                report.rejection_reasons["conversion_failed"] = (
                    report.rejection_reasons.get("conversion_failed", 0) + 1
                )
                continue

            samples.append(sample)
            report.exported += 1

            # 标记已处理
            if messages:
                self._cleaner.mark_seen(messages)
            self._tracker.mark_exported(trajectory.trajectory_id)

        # 写入输出文件
        if samples:
            output_file = self._write_samples(samples, report)
            report.output_file = str(output_file)

        # 持久化追踪数据
        self._tracker.save()
        self._cleaner.save_hashes()

        report.completed_at = datetime.now()
        report.duration_seconds = (report.completed_at - report.started_at).total_seconds()

        logger.info(
            f"Export completed: {report.exported} exported, "
            f"{report.rejected} rejected, {report.duplicates} duplicates, "
            f"{report.skipped_incremental} skipped (incremental). "
            f"Output: {report.output_file}"
        )

        return report

    async def export_async(
        self,
        trajectories: list[Trajectory],
        progress_callback: Optional[Any] = None,
    ) -> ExportReport:
        """异步导出数据集，支持进度回调。

        Args:
            trajectories: 待处理的轨迹列表
            progress_callback: 可选的进度回调函数 (processed, total) -> None

        Returns:
            导出报告
        """
        report = ExportReport(
            format=self._config.format,
            started_at=datetime.now(),
        )

        output_dir = Path(self._config.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # 增量过滤
        candidates = trajectories
        if self._config.incremental:
            candidates = [t for t in trajectories if not self._tracker.is_exported(t.trajectory_id)]
            report.skipped_incremental = len(trajectories) - len(candidates)

        logger.info(
            f"Async export: {len(candidates)} candidates "
            f"(incremental skipped: {report.skipped_incremental})"
        )

        # 分批处理
        samples: list[dict[str, Any]] = []
        batch_size = self._config.batch_size
        semaphore = asyncio.Semaphore(self._config.max_concurrent)

        async def process_trajectory(
            trajectory: Trajectory,
        ) -> dict[str, Any] | None:
            async with semaphore:
                return await asyncio.to_thread(self._process_single, trajectory, report)

        for batch_start in range(0, len(candidates), batch_size):
            batch = candidates[batch_start : batch_start + batch_size]
            tasks = [process_trajectory(t) for t in batch]
            results = await asyncio.gather(*tasks)

            for result in results:
                if result is not None:
                    samples.append(result)

            if progress_callback:
                processed = min(batch_start + batch_size, len(candidates))
                await progress_callback(processed, len(candidates))

        # 写入输出文件
        if samples:
            output_file = self._write_samples(samples, report)
            report.output_file = str(output_file)

        # 持久化
        self._tracker.save()
        self._cleaner.save_hashes()

        report.completed_at = datetime.now()
        report.duration_seconds = (report.completed_at - report.started_at).total_seconds()

        logger.info(
            f"Async export completed: {report.exported} exported, "
            f"{report.rejected} rejected, {report.duplicates} duplicates. "
            f"Output: {report.output_file}"
        )

        return report

    def _process_single(
        self, trajectory: Trajectory, report: ExportReport
    ) -> dict[str, Any] | None:
        """处理单条轨迹（线程安全版本，更新 report 的计数器需外部同步）。"""
        report.total_processed += 1

        # 质量过滤
        passed, score, reasons = self._quality_filter.filter_trajectory(trajectory)
        if not passed:
            report.rejected += 1
            for reason in reasons:
                category = reason.split(":")[0]
                report.rejection_reasons[category] = report.rejection_reasons.get(category, 0) + 1
            return None

        report.quality_scores.append(score)

        # 构建消息
        messages = self._converter._trajectory_to_messages(trajectory)
        if messages:
            messages = self._cleaner.clean_messages(messages)

        # 去重
        if messages and self._cleaner.is_duplicate(messages):
            report.duplicates += 1
            return None

        # 格式转换
        sample = self._converter.convert(trajectory)
        if sample is None:
            report.rejected += 1
            return None

        report.exported += 1

        # 标记已处理
        if messages:
            self._cleaner.mark_seen(messages)
        self._tracker.mark_exported(trajectory.trajectory_id)

        return sample

    def _write_samples(self, samples: list[dict[str, Any]], report: ExportReport) -> Path:
        """将样本写入 JSONL 文件。"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        format_name = self._config.format.value
        filename = f"{self._config.file_prefix}_{format_name}_{timestamp}.jsonl"
        output_file = Path(self._config.output_dir) / filename

        with open(output_file, "w", encoding="utf-8") as f:
            for sample in samples:
                line = json.dumps(sample, ensure_ascii=False)
                f.write(line + "\n")

        report.file_size_bytes = output_file.stat().st_size
        logger.info(
            f"Wrote {len(samples)} samples to {output_file} ({report.file_size_bytes} bytes)"
        )

        # 同时写入元数据文件
        meta_file = output_file.with_suffix(".meta.json")
        meta = {
            "format": format_name,
            "sample_count": len(samples),
            "file_size_bytes": report.file_size_bytes,
            "created_at": datetime.now().isoformat(),
            "config": self._config.model_dump(),
        }
        with open(meta_file, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        return output_file

    def export_from_jsonl(
        self, input_file: str | Path, output_file: str | Path | None = None
    ) -> ExportReport:
        """从已有的轨迹 JSONL 文件导出数据集。

        用于离线批量处理已存储的轨迹数据。

        Args:
            input_file: 轨迹 JSONL 文件路径
            output_file: 可选的输出文件路径

        Returns:
            导出报告
        """
        input_path = Path(input_file)
        if not input_path.exists():
            raise FileNotFoundError(f"Input file not found: {input_path}")

        trajectories: list[Trajectory] = []
        with open(input_path, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    trajectory = Trajectory(**data)
                    trajectories.append(trajectory)
                except (json.JSONDecodeError, Exception) as exc:
                    logger.warning(f"Failed to parse line {line_num} in {input_path}: {exc}")

        logger.info(f"Loaded {len(trajectories)} trajectories from {input_path}")

        report = self.export(trajectories)

        # 如果指定了输出文件，移动结果
        if output_file and report.output_file:
            dest = Path(output_file)
            dest.parent.mkdir(parents=True, exist_ok=True)
            src = Path(report.output_file)
            if src.exists():
                src.rename(dest)
                report.output_file = str(dest)

        return report


# =============================================================================
# 9. 导出报告
# =============================================================================


class ExportReport(BaseModel):
    """导出操作报告。"""

    format: ExportFormat = Field(description="导出格式")
    started_at: datetime = Field(description="开始时间")
    completed_at: Optional[datetime] = Field(default=None, description="完成时间")
    duration_seconds: float = Field(default=0.0, description="耗时（秒）")

    total_processed: int = Field(default=0, description="总处理数")
    exported: int = Field(default=0, description="成功导出数")
    rejected: int = Field(default=0, description="被拒绝数")
    duplicates: int = Field(default=0, description="重复数")
    skipped_incremental: int = Field(default=0, description="增量跳过数")

    quality_scores: list[float] = Field(default_factory=list, description="通过的质量评分列表")
    rejection_reasons: dict[str, int] = Field(default_factory=dict, description="拒绝原因统计")

    output_file: Optional[str] = Field(default=None, description="输出文件路径")
    file_size_bytes: int = Field(default=0, description="输出文件大小（字节）")

    @property
    def average_quality_score(self) -> float:
        """平均质量评分。"""
        if not self.quality_scores:
            return 0.0
        return sum(self.quality_scores) / len(self.quality_scores)

    @property
    def acceptance_rate(self) -> float:
        """通过率。"""
        if self.total_processed == 0:
            return 0.0
        return self.exported / self.total_processed

    def summary(self) -> str:
        """生成人类可读的报告摘要。"""
        lines = [
            "=" * 60,
            f"Dataset Export Report ({self.format.value})",
            "=" * 60,
            f"Duration:       {self.duration_seconds:.1f}s",
            f"Total processed: {self.total_processed}",
            f"Exported:       {self.exported}",
            f"Rejected:       {self.rejected}",
            f"Duplicates:     {self.duplicates}",
            f"Incremental skip: {self.skipped_incremental}",
            f"Acceptance rate: {self.acceptance_rate:.1%}",
            f"Avg quality:    {self.average_quality_score:.3f}",
        ]

        if self.rejection_reasons:
            lines.append("Rejection reasons:")
            for reason, count in sorted(self.rejection_reasons.items(), key=lambda x: -x[1]):
                lines.append(f"  - {reason}: {count}")

        if self.output_file:
            lines.append(f"Output file:    {self.output_file}")
            lines.append(f"File size:      {self.file_size_bytes:,} bytes")

        lines.append("=" * 60)
        return "\n".join(lines)
