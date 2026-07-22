"""共识辩论引擎 - Proposer/Critic/Refiner 三角色多轮辩论与共识检测"""

from __future__ import annotations

import json
import time
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional
from uuid import uuid4

import httpx
from pydantic import BaseModel, Field

from symbio.config.settings import get_settings
from symbio.utils.logger import get_logger

logger = get_logger("agents.debate")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


class DebateRole(str, Enum):
    """辩论角色"""

    PROPOSER = "proposer"
    CRITIC = "critic"
    REFINER = "refiner"


class DebateRoundStatus(str, Enum):
    """单轮辩论状态"""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CONSENSUS_REACHED = "consensus_reached"


class DebateStatus(str, Enum):
    """辩论整体状态"""

    CREATED = "created"
    RUNNING = "running"
    CONSENSUS = "consensus"
    MAX_ROUNDS_REACHED = "max_rounds_reached"
    FAILED = "failed"


class Argument(BaseModel):
    """单次发言"""

    argument_id: str = Field(default_factory=lambda: str(uuid4()))
    role: DebateRole
    round_number: int
    content: str
    reasoning: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    timestamp: datetime = Field(default_factory=datetime.now)
    references: list[str] = Field(default_factory=list)


class DebateRound(BaseModel):
    """一轮辩论"""

    round_id: str = Field(default_factory=lambda: str(uuid4()))
    round_number: int
    status: DebateRoundStatus = DebateRoundStatus.PENDING
    proposal: Optional[Argument] = None
    critique: Optional[Argument] = None
    refinement: Optional[Argument] = None
    consensus_score: float = Field(default=0.0, ge=0.0, le=1.0)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class ConsensusResult(BaseModel):
    """共识检测结果"""

    is_consensus: bool
    score: float = Field(ge=0.0, le=1.0)
    summary: str = ""
    key_agreements: list[str] = Field(default_factory=list)
    remaining_disagreements: list[str] = Field(default_factory=list)


class DebateSession(BaseModel):
    """辩论会话"""

    session_id: str = Field(default_factory=lambda: str(uuid4()))
    topic: str
    status: DebateStatus = DebateStatus.CREATED
    max_rounds: int = Field(default=5, ge=1)
    consensus_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    rounds: list[DebateRound] = Field(default_factory=list)
    final_proposal: str = ""
    final_consensus: Optional[ConsensusResult] = None
    created_at: datetime = Field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 角色策略接口
# ---------------------------------------------------------------------------


class RoleStrategy:
    """角色策略基类 - 定义各角色的推理行为"""

    def __init__(self, role: DebateRole):
        self.role = role

    async def generate_argument(
        self,
        topic: str,
        history: list[DebateRound],
        current_proposal: str,
    ) -> Argument:
        """生成发言内容 - 子类必须实现"""
        raise NotImplementedError


class ProposerStrategy(RoleStrategy):
    """提案者策略 - 生成或改进提案"""

    def __init__(self, generator: Callable[..., str] | None = None):
        super().__init__(DebateRole.PROPOSER)
        self._generator = generator

    async def generate_argument(
        self,
        topic: str,
        history: list[DebateRound],
        current_proposal: str,
    ) -> Argument:
        if self._generator:
            content = self._generator(topic=topic, history=history, proposal=current_proposal)
        else:
            content = f"针对主题「{topic}」的提案: {current_proposal or '需要制定详细方案'}"

        return Argument(
            role=self.role,
            round_number=len(history) + 1,
            content=content,
            reasoning="基于主题分析和已有讨论生成提案",
            confidence=0.7,
        )


class CriticStrategy(RoleStrategy):
    """批评者策略 - 审查并提出问题"""

    def __init__(self, generator: Callable[..., str] | None = None):
        super().__init__(DebateRole.CRITIC)
        self._generator = generator

    async def generate_argument(
        self,
        topic: str,
        history: list[DebateRound],
        current_proposal: str,
    ) -> Argument:
        if self._generator:
            content = self._generator(topic=topic, history=history, proposal=current_proposal)
        else:
            content = f"对当前提案「{current_proposal}」的审查意见: 需要进一步完善细节和边界条件"

        return Argument(
            role=self.role,
            round_number=len(history) + 1,
            content=content,
            reasoning="对提案进行批判性分析",
            confidence=0.6,
        )


class RefinerStrategy(RoleStrategy):
    """精炼者策略 - 综合提案和批评产出改进方案"""

    def __init__(self, generator: Callable[..., str] | None = None):
        super().__init__(DebateRole.REFINER)
        self._generator = generator

    async def generate_argument(
        self,
        topic: str,
        history: list[DebateRound],
        current_proposal: str,
    ) -> Argument:
        if self._generator:
            content = self._generator(topic=topic, history=history, proposal=current_proposal)
        else:
            content = f"综合讨论后改进方案: 在「{current_proposal}」基础上进行优化"

        return Argument(
            role=self.role,
            round_number=len(history) + 1,
            content=content,
            reasoning="综合提案和批评意见进行精炼",
            confidence=0.75,
        )


# ---------------------------------------------------------------------------
# LLM 驱动策略
# ---------------------------------------------------------------------------


class _LLMRoleStrategy(RoleStrategy):
    """LLM 驱动角色策略基类 - 通过 Anthropic API 生成真实推理内容"""

    # 子类必须覆盖
    _system_prompt: str = ""

    def __init__(self, role: DebateRole, settings: Any | None = None):
        super().__init__(role)
        self._settings = settings or get_settings()
        self._call_count: int = 0
        self._total_tokens: int = 0

    @property
    def call_count(self) -> int:
        return self._call_count

    @property
    def total_tokens(self) -> int:
        return self._total_tokens

    def _format_history(self, history: list[DebateRound]) -> str:
        """将辩论历史格式化为可读上下文"""
        if not history:
            return "No previous rounds."
        lines: list[str] = []
        for r in history:
            lines.append(f"Round {r.round_number}:")
            if r.proposal:
                lines.append(f"  - Proposer: {r.proposal.content}")
            if r.critique:
                lines.append(f"  - Critic: {r.critique.content}")
            if r.refinement:
                lines.append(f"  - Refiner: {r.refinement.content}")
            lines.append(f"  Consensus score: {r.consensus_score:.2f}")
            lines.append("")
        return "\n".join(lines)

    async def _call_llm(self, user_message: str) -> dict[str, Any] | None:
        """调用 Anthropic Messages API 并返回解析后的 JSON dict

        Returns:
            解析成功返回 dict，失败返回 None
        """
        mc = self._settings.model
        if not mc.anthropic_api_key:
            logger.debug(f"{self.role.value} LLM 策略: 无 API key，将降级到模板")
            return None

        try:
            url = mc.anthropic_base_url.rstrip("/") + "/v1/messages"
            headers = {
                "x-api-key": mc.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            body = {
                "model": mc.model_low,
                "max_tokens": 1024,
                "system": self._system_prompt,
                "messages": [{"role": "user", "content": user_message}],
            }

            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(url, json=body, headers=headers)
                resp.raise_for_status()

            data = resp.json()
            text = data["content"][0]["text"].strip()

            # 追踪 token 使用
            usage = data.get("usage", {})
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            self._total_tokens += input_tokens + output_tokens
            self._call_count += 1

            # 尝试从 JSON 块或纯文本中提取 JSON
            return self._parse_json_response(text)

        except Exception as exc:
            logger.warning(f"{self.role.value} LLM 调用失败: {exc}")
            return None

    @staticmethod
    def _parse_json_response(text: str) -> dict[str, Any] | None:
        """从 LLM 响应文本中提取 JSON 对象"""
        # 先尝试直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 尝试从 ```json ... ``` 代码块中提取
        if "```" in text:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    return json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    pass

        # 尝试找到第一个 { 到最后一个 }
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass

        logger.debug(f"无法从 LLM 响应中提取 JSON: {text[:200]}")
        return None

    def _build_user_message(
        self,
        topic: str,
        history: list[DebateRound],
        current_proposal: str,
    ) -> str:
        """构造发送给 LLM 的用户消息"""
        history_text = self._format_history(history)
        return (
            f"Debate topic: {topic}\n\n"
            f"Current proposal: {current_proposal or '(none yet)'}\n\n"
            f"Debate history:\n{history_text}\n\n"
            f"Please respond with your JSON output."
        )

    async def generate_argument(
        self,
        topic: str,
        history: list[DebateRound],
        current_proposal: str,
    ) -> Argument:
        """生成发言 - 优先使用 LLM，失败则降级到模板"""
        user_message = self._build_user_message(topic, history, current_proposal)
        llm_result = await self._call_llm(user_message)

        if llm_result and isinstance(llm_result, dict):
            content = llm_result.get("content", "")
            reasoning = llm_result.get("reasoning", "")
            confidence_raw = llm_result.get("confidence", 0.5)
            try:
                confidence = float(confidence_raw)
                confidence = max(0.0, min(1.0, confidence))
            except (TypeError, ValueError):
                confidence = 0.5

            if content:
                return Argument(
                    role=self.role,
                    round_number=len(history) + 1,
                    content=content,
                    reasoning=reasoning or f"LLM 驱动的{self.role.value}分析",
                    confidence=confidence,
                )

        # 降级到模板策略
        logger.info(f"{self.role.value} LLM 未返回有效内容，降级到模板策略")
        return await self._fallback_argument(topic, history, current_proposal)

    async def _fallback_argument(
        self,
        topic: str,
        history: list[DebateRound],
        current_proposal: str,
    ) -> Argument:
        """子类必须实现的模板降级逻辑"""
        raise NotImplementedError


class LLMProposerStrategy(_LLMRoleStrategy):
    """LLM 驱动的提案者策略"""

    _system_prompt = (
        "You are the PROPOSER in a multi-agent debate. "
        "Your job is to create or improve a proposal.\n"
        "Be specific, actionable, and thorough. Support your ideas with reasoning.\n"
        'Respond in JSON: {"content": "your proposal", "reasoning": "your reasoning", "confidence": 0.0-1.0}'
    )

    def __init__(self, settings: Any | None = None):
        super().__init__(DebateRole.PROPOSER, settings)

    async def _fallback_argument(
        self,
        topic: str,
        history: list[DebateRound],
        current_proposal: str,
    ) -> Argument:
        content = f"针对主题「{topic}」的提案: {current_proposal or '需要制定详细方案'}"
        return Argument(
            role=self.role,
            round_number=len(history) + 1,
            content=content,
            reasoning="基于主题分析和已有讨论生成提案",
            confidence=0.7,
        )


class LLMCriticStrategy(_LLMRoleStrategy):
    """LLM 驱动的批评者策略"""

    _system_prompt = (
        "You are the CRITIC in a multi-agent debate. "
        "Your job is to identify weaknesses, risks, and gaps.\n"
        "Be constructive but thorough. Point out specific issues and suggest improvements.\n"
        'Respond in JSON: {"content": "your critique", "reasoning": "your reasoning", "confidence": 0.0-1.0}'
    )

    def __init__(self, settings: Any | None = None):
        super().__init__(DebateRole.CRITIC, settings)

    async def _fallback_argument(
        self,
        topic: str,
        history: list[DebateRound],
        current_proposal: str,
    ) -> Argument:
        content = f"对当前提案「{current_proposal}」的审查意见: 需要进一步完善细节和边界条件"
        return Argument(
            role=self.role,
            round_number=len(history) + 1,
            content=content,
            reasoning="对提案进行批判性分析",
            confidence=0.6,
        )


class LLMRefinerStrategy(_LLMRoleStrategy):
    """LLM 驱动的精炼者策略"""

    _system_prompt = (
        "You are the REFINER in a multi-agent debate. "
        "Synthesize the proposal and critique into an improved version.\n"
        "Address the critic's concerns while preserving the proposal's strengths.\n"
        'Respond in JSON: {"content": "refined proposal", "reasoning": "your reasoning", "confidence": 0.0-1.0}'
    )

    def __init__(self, settings: Any | None = None):
        super().__init__(DebateRole.REFINER, settings)

    async def _fallback_argument(
        self,
        topic: str,
        history: list[DebateRound],
        current_proposal: str,
    ) -> Argument:
        content = f"综合讨论后改进方案: 在「{current_proposal}」基础上进行优化"
        return Argument(
            role=self.role,
            round_number=len(history) + 1,
            content=content,
            reasoning="综合提案和批评意见进行精炼",
            confidence=0.75,
        )


# ---------------------------------------------------------------------------
# 共识检测器
# ---------------------------------------------------------------------------


class ConsensusDetector:
    """共识检测器 - 通过文本相似度和置信度判断是否达成共识"""

    def __init__(self, threshold: float = 0.8):
        self.threshold = threshold

    def detect(
        self,
        proposal: Argument,
        critique: Argument,
        refinement: Argument,
        history: list[DebateRound],
    ) -> ConsensusResult:
        """检测当前轮是否达成共识

        基于以下指标综合评估:
        1. 提案与精炼方案的内容重叠度
        2. 批评意见的严重程度
        3. 各角色置信度收敛程度
        4. 历史轮次的趋势
        """
        # 计算内容重叠度 (简化版: 基于关键词集合交集)
        proposal_words = set(proposal.content)
        refinement_words = set(refinement.content)
        if proposal_words or refinement_words:
            overlap = len(proposal_words & refinement_words) / max(
                len(proposal_words | refinement_words), 1
            )
        else:
            overlap = 0.0

        # 置信度收敛
        confidences = [proposal.confidence, critique.confidence, refinement.confidence]
        avg_confidence = sum(confidences) / len(confidences)
        confidence_variance = sum((c - avg_confidence) ** 2 for c in confidences) / len(confidences)
        confidence_score = max(0.0, 1.0 - confidence_variance * 4)

        # 历史趋势: 如果前面几轮分数在上升, 给予加成
        trend_bonus = 0.0
        if len(history) >= 2:
            recent_scores = [r.consensus_score for r in history[-2:]]
            if all(recent_scores[i] <= recent_scores[i + 1] for i in range(len(recent_scores) - 1)):
                trend_bonus = 0.1

        # 综合得分
        score = min(1.0, overlap * 0.4 + confidence_score * 0.4 + trend_bonus + 0.1)

        # 提取共识要点和分歧
        key_agreements: list[str] = []
        remaining_disagreements: list[str] = []

        if score >= 0.5:
            key_agreements.append("方案核心框架已达成一致")
        if refinement.confidence >= 0.7:
            key_agreements.append("精炼方案置信度较高")
        if critique.confidence < 0.5:
            remaining_disagreements.append("批评者仍存在较多疑虑")

        is_consensus = score >= self.threshold
        summary = (
            f"共识得分: {score:.2f} (阈值: {self.threshold}), "
            f"{'已达成共识' if is_consensus else '尚未达成共识'}"
        )

        return ConsensusResult(
            is_consensus=is_consensus,
            score=score,
            summary=summary,
            key_agreements=key_agreements,
            remaining_disagreements=remaining_disagreements,
        )


# ---------------------------------------------------------------------------
# 辩论引擎
# ---------------------------------------------------------------------------


class DebateEngine:
    """共识辩论引擎

    支持 Proposer/Critic/Refiner 三角色多轮辩论, 自动检测共识并终止。

    用法:
        engine = DebateEngine()
        session = await engine.run_debate("设计一个微服务架构方案")
    """

    def __init__(
        self,
        proposer: RoleStrategy | None = None,
        critic: RoleStrategy | None = None,
        refiner: RoleStrategy | None = None,
        consensus_detector: ConsensusDetector | None = None,
        max_rounds: int = 5,
        consensus_threshold: float = 0.8,
        use_llm: bool = True,
    ):
        self._use_llm = use_llm
        if proposer is not None:
            self.proposer = proposer
        elif use_llm:
            self.proposer = LLMProposerStrategy()
        else:
            self.proposer = ProposerStrategy()

        if critic is not None:
            self.critic = critic
        elif use_llm:
            self.critic = LLMCriticStrategy()
        else:
            self.critic = CriticStrategy()

        if refiner is not None:
            self.refiner = refiner
        elif use_llm:
            self.refiner = LLMRefinerStrategy()
        else:
            self.refiner = RefinerStrategy()

        self.consensus_detector = consensus_detector or ConsensusDetector(
            threshold=consensus_threshold
        )
        self.max_rounds = max_rounds
        self.consensus_threshold = consensus_threshold
        self._sessions: dict[str, DebateSession] = {}

    async def run_debate(
        self,
        topic: str,
        initial_proposal: str = "",
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DebateSession:
        """运行完整辩论流程

        Args:
            topic: 辩论主题
            initial_proposal: 初始提案 (可选)
            session_id: 指定会话 ID (可选)
            metadata: 附加元数据

        Returns:
            完成的辩论会话
        """
        session = DebateSession(
            session_id=session_id or str(uuid4()),
            topic=topic,
            max_rounds=self.max_rounds,
            consensus_threshold=self.consensus_threshold,
            metadata=metadata or {},
        )
        self._sessions[session.session_id] = session
        session.status = DebateStatus.RUNNING

        logger.info(
            f"辩论开始: session={session.session_id}, topic={topic}, max_rounds={self.max_rounds}"
        )

        current_proposal = initial_proposal
        round_durations: list[float] = []

        for round_num in range(1, self.max_rounds + 1):
            logger.info(f"辩论第 {round_num} 轮开始")
            round_start = time.monotonic()

            debate_round = DebateRound(
                round_number=round_num,
                status=DebateRoundStatus.IN_PROGRESS,
                started_at=datetime.now(),
            )

            # Phase 1: Proposer 提案
            proposal_arg = await self.proposer.generate_argument(
                topic=topic,
                history=session.rounds,
                current_proposal=current_proposal,
            )
            debate_round.proposal = proposal_arg
            current_proposal = proposal_arg.content

            # Phase 2: Critic 批评
            critique_arg = await self.critic.generate_argument(
                topic=topic,
                history=session.rounds,
                current_proposal=current_proposal,
            )
            debate_round.critique = critique_arg

            # Phase 3: Refiner 精炼
            refinement_arg = await self.refiner.generate_argument(
                topic=topic,
                history=session.rounds,
                current_proposal=current_proposal,
            )
            debate_round.refinement = refinement_arg
            current_proposal = refinement_arg.content

            # 共识检测
            consensus = self.consensus_detector.detect(
                proposal=proposal_arg,
                critique=critique_arg,
                refinement=refinement_arg,
                history=session.rounds,
            )
            debate_round.consensus_score = consensus.score
            debate_round.status = DebateRoundStatus.COMPLETED
            debate_round.completed_at = datetime.now()

            round_elapsed = time.monotonic() - round_start
            round_durations.append(round_elapsed)

            session.rounds.append(debate_round)
            logger.info(
                f"辩论第 {round_num} 轮完成: consensus_score={consensus.score:.2f}, "
                f"is_consensus={consensus.is_consensus}, duration={round_elapsed:.2f}s"
            )

            if consensus.is_consensus:
                debate_round.status = DebateRoundStatus.CONSENSUS_REACHED
                session.status = DebateStatus.CONSENSUS
                session.final_proposal = current_proposal
                session.final_consensus = consensus
                session.completed_at = datetime.now()
                self._add_debate_summary(session, round_durations)
                logger.info(f"辩论达成共识: session={session.session_id}, rounds={round_num}")
                return session

        # 达到最大轮次
        session.status = DebateStatus.MAX_ROUNDS_REACHED
        session.final_proposal = current_proposal
        session.final_consensus = ConsensusResult(
            is_consensus=False,
            score=session.rounds[-1].consensus_score if session.rounds else 0.0,
            summary=f"达到最大轮数 {self.max_rounds} 仍未达成共识",
        )
        session.completed_at = datetime.now()
        self._add_debate_summary(session, round_durations)
        logger.warning(
            f"辩论未达成共识: session={session.session_id}, reached max rounds={self.max_rounds}"
        )
        return session

    def _add_debate_summary(self, session: DebateSession, round_durations: list[float]) -> None:
        """将辩论摘要（LLM 调用次数、token 用量、各轮耗时）写入 session.metadata"""
        total_llm_calls = 0
        total_tokens = 0
        for strategy in (self.proposer, self.critic, self.refiner):
            if isinstance(strategy, _LLMRoleStrategy):
                total_llm_calls += strategy.call_count
                total_tokens += strategy.total_tokens

        summary: dict[str, Any] = {
            "total_llm_calls": total_llm_calls,
            "total_tokens_used": total_tokens,
            "round_durations_sec": [round(d, 3) for d in round_durations],
            "total_duration_sec": round(sum(round_durations), 3),
        }
        session.metadata["debate_summary"] = summary

    def get_session(self, session_id: str) -> DebateSession | None:
        """获取辩论会话"""
        return self._sessions.get(session_id)

    def list_sessions(self) -> list[DebateSession]:
        """列出所有辩论会话"""
        return list(self._sessions.values())

    def get_debate_history(self, session_id: str) -> list[dict[str, Any]]:
        """获取辩论历史记录 (序列化格式)"""
        session = self._sessions.get(session_id)
        if not session:
            return []

        history: list[dict[str, Any]] = []
        for debate_round in session.rounds:
            round_data: dict[str, Any] = {
                "round": debate_round.round_number,
                "consensus_score": debate_round.consensus_score,
                "status": debate_round.status.value,
            }
            if debate_round.proposal:
                round_data["proposal"] = {
                    "content": debate_round.proposal.content,
                    "confidence": debate_round.proposal.confidence,
                }
            if debate_round.critique:
                round_data["critique"] = {
                    "content": debate_round.critique.content,
                    "confidence": debate_round.critique.confidence,
                }
            if debate_round.refinement:
                round_data["refinement"] = {
                    "content": debate_round.refinement.content,
                    "confidence": debate_round.refinement.confidence,
                }
            history.append(round_data)

        return history


# 别名，保持向后兼容
MultiAgentDebate = DebateEngine
