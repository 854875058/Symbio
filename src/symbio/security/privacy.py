"""隐私计算模块 - 联邦学习基础框架与差分隐私"""

from __future__ import annotations

import hashlib
import math
import os
import random
from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

import numpy as np
from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

logger = get_logger("security.privacy")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

class PrivacyMechanism(str, Enum):
    """隐私机制类型"""
    LAPLACE = "laplace"
    GAUSSIAN = "gaussian"
    EXPONENTIAL = "exponential"


class AggregationStrategy(str, Enum):
    """联邦聚合策略"""
    FEDERATED_AVERAGING = "federated_averaging"
    WEIGHTED_AVERAGING = "weighted_averaging"
    FEDERATED_PROX = "federated_prox"


class ClientStatus(str, Enum):
    """联邦学习客户端状态"""
    IDLE = "idle"
    TRAINING = "training"
    UPLOADING = "uploading"
    COMPLETED = "completed"
    FAILED = "failed"


class RoundStatus(str, Enum):
    """训练轮次状态"""
    WAITING = "waiting"
    COLLECTING = "collecting"
    AGGREGATING = "aggregating"
    COMPLETED = "completed"
    FAILED = "failed"


class DifferentialPrivacyParams(BaseModel):
    """差分隐私参数"""
    epsilon: float = Field(default=1.0, gt=0, description="隐私预算 epsilon")
    delta: float = Field(default=1e-5, gt=0, description="松弛参数 delta")
    mechanism: PrivacyMechanism = PrivacyMechanism.GAUSSIAN
    max_grad_norm: float = Field(default=1.0, gt=0, description="梯度裁剪范数上限")
    noise_multiplier: float = Field(default=1.1, gt=0, description="噪声乘数")
    secure_rng: bool = Field(default=False, description="是否使用安全随机数生成器")


class ClientUpdate(BaseModel):
    """客户端模型更新"""
    client_id: str
    round_id: int
    parameters: list[list[float]] = Field(default_factory=list, description="模型参数 (各层)")
    num_samples: int = 0
    loss: float = 0.0
    duration_ms: float = 0.0
    status: ClientStatus = ClientStatus.IDLE
    metadata: dict[str, Any] = Field(default_factory=dict)


class FederatedRound(BaseModel):
    """联邦训练轮次"""
    round_id: int
    status: RoundStatus = RoundStatus.WAITING
    selected_clients: list[str] = Field(default_factory=list)
    client_updates: list[ClientUpdate] = Field(default_factory=list)
    global_parameters: list[list[float]] = Field(default_factory=list)
    global_loss: float = 0.0
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class FederatedSession(BaseModel):
    """联邦学习会话"""
    session_id: str = Field(default_factory=lambda: str(uuid4()))
    description: str = ""
    num_clients: int = 0
    num_rounds: int = 0
    current_round: int = 0
    aggregation_strategy: AggregationStrategy = AggregationStrategy.FEDERATED_AVERAGING
    privacy_params: DifferentialPrivacyParams = Field(default_factory=DifferentialPrivacyParams)
    rounds: list[FederatedRound] = Field(default_factory=list)
    global_parameters: list[list[float]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PrivacyAuditRecord(BaseModel):
    """隐私审计记录"""
    record_id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str
    round_id: int
    mechanism: PrivacyMechanism
    epsilon_spent: float = 0.0
    delta_used: float = 0.0
    noise_scale: float = 0.0
    gradient_norm_before: float = 0.0
    gradient_norm_after: float = 0.0
    timestamp: datetime = Field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# 差分隐私引擎
# ---------------------------------------------------------------------------

class DifferentialPrivacyEngine:
    """差分隐私引擎

    支持 Laplace、Gaussian 和指数机制, 提供梯度裁剪和噪声注入。

    用法:
        dp = DifferentialPrivacyEngine(epsilon=1.0, delta=1e-5)
        noisy_grad = dp.add_noise(gradient)
    """

    def __init__(
        self,
        epsilon: float = 1.0,
        delta: float = 1e-5,
        mechanism: PrivacyMechanism = PrivacyMechanism.GAUSSIAN,
        max_grad_norm: float = 1.0,
        noise_multiplier: float = 1.1,
    ):
        self.epsilon = epsilon
        self.delta = delta
        self.mechanism = mechanism
        self.max_grad_norm = max_grad_norm
        self.noise_multiplier = noise_multiplier
        self._total_epsilon_spent: float = 0.0
        self._audit_records: list[PrivacyAuditRecord] = []

    @property
    def remaining_budget(self) -> float:
        """剩余隐私预算"""
        return max(0.0, self.epsilon - self._total_epsilon_spent)

    def clip_gradients(self, gradients: np.ndarray) -> np.ndarray:
        """梯度裁剪

        将梯度的 L2 范数裁剪到 max_grad_norm 以内。

        Args:
            gradients: 原始梯度

        Returns:
            裁剪后的梯度
        """
        grad_norm = np.linalg.norm(gradients)
        if grad_norm > self.max_grad_norm:
            gradients = gradients * (self.max_grad_norm / grad_norm)
        return gradients

    def add_laplace_noise(self, value: float, sensitivity: float = 1.0) -> float:
        """添加 Laplace 噪声

        Args:
            value: 原始值
            sensitivity: 查询敏感度

        Returns:
            添加噪声后的值
        """
        scale = sensitivity / self.epsilon
        noise = np.random.laplace(0, scale)
        return float(value + noise)

    def add_gaussian_noise(
        self,
        value: float | np.ndarray,
        sensitivity: float = 1.0,
    ) -> float | np.ndarray:
        """添加高斯噪声

        使用 (epsilon, delta)-差分隐私的高斯机制。

        Args:
            value: 原始值或数组
            sensitivity: 查询敏感度

        Returns:
            添加噪声后的值
        """
        sigma = self.noise_multiplier * sensitivity * math.sqrt(2 * math.log(1.25 / self.delta)) / self.epsilon
        if isinstance(value, np.ndarray):
            noise = np.random.normal(0, sigma, value.shape)
        else:
            noise = np.random.normal(0, sigma)
        return value + noise

    def add_noise(
        self,
        data: np.ndarray,
        sensitivity: float = 1.0,
    ) -> np.ndarray:
        """根据配置的机制添加噪声

        Args:
            data: 原始数据
            sensitivity: 敏感度

        Returns:
            添加噪声后的数据
        """
        grad_norm_before = float(np.linalg.norm(data))

        # 梯度裁剪
        clipped = self.clip_gradients(data)
        grad_norm_after = float(np.linalg.norm(clipped))

        # 添加噪声
        if self.mechanism == PrivacyMechanism.LAPLACE:
            scale = sensitivity / self.epsilon
            noise = np.random.laplace(0, scale, data.shape)
            result = clipped + noise
        elif self.mechanism == PrivacyMechanism.GAUSSIAN:
            sigma = self.noise_multiplier * sensitivity * math.sqrt(2 * math.log(1.25 / self.delta)) / self.epsilon
            noise = np.random.normal(0, sigma, data.shape)
            result = clipped + noise
        elif self.mechanism == PrivacyMechanism.EXPONENTIAL:
            # 指数机制: 对每个元素独立应用
            sigma = sensitivity / self.epsilon
            noise = np.random.exponential(sigma, data.shape) - sigma
            result = clipped + noise
        else:
            raise ValueError(f"不支持的隐私机制: {self.mechanism}")

        # 更新预算
        self._total_epsilon_spent += self.epsilon
        noise_scale = float(np.std(noise)) if isinstance(noise, np.ndarray) else float(abs(noise))

        logger.debug(
            f"差分隐私噪声添加: mechanism={self.mechanism.value}, "
            f"epsilon={self.epsilon}, noise_scale={noise_scale:.4f}"
        )

        return result

    def exponential_mechanism(
        self,
        candidates: list[Any],
        utility_scores: list[float],
        sensitivity: float = 1.0,
    ) -> Any:
        """指数机制 - 从候选集中按效用概率采样

        Args:
            candidates: 候选对象列表
            utility_scores: 对应的效用分数
            sensitivity: 效用函数敏感度

        Returns:
            采样选中的候选对象
        """
        if not candidates:
            raise ValueError("候选列表不能为空")

        scores = np.array(utility_scores, dtype=float)
        probabilities = np.exp(self.epsilon * scores / (2 * sensitivity))
        probabilities = probabilities / probabilities.sum()

        idx = np.random.choice(len(candidates), p=probabilities)
        return candidates[idx]

    def create_audit_record(
        self,
        session_id: str,
        round_id: int,
        noise_scale: float,
        grad_norm_before: float,
        grad_norm_after: float,
    ) -> PrivacyAuditRecord:
        """创建隐私审计记录"""
        record = PrivacyAuditRecord(
            session_id=session_id,
            round_id=round_id,
            mechanism=self.mechanism,
            epsilon_spent=self.epsilon,
            delta_used=self.delta,
            noise_scale=noise_scale,
            gradient_norm_before=grad_norm_before,
            gradient_norm_after=grad_norm_after,
        )
        self._audit_records.append(record)
        return record

    def get_audit_records(self) -> list[PrivacyAuditRecord]:
        """获取审计记录"""
        return list(self._audit_records)


# ---------------------------------------------------------------------------
# 联邦学习客户端
# ---------------------------------------------------------------------------

class FederatedClient:
    """联邦学习客户端

    模拟单个参与方的本地训练和参数上传。
    """

    def __init__(
        self,
        client_id: str,
        dp_engine: DifferentialPrivacyEngine | None = None,
    ):
        self.client_id = client_id
        self.dp_engine = dp_engine
        self.status = ClientStatus.IDLE
        self._local_data_size: int = 0
        self._local_parameters: list[list[float]] = []

    def set_local_data(self, num_samples: int) -> None:
        """设置本地数据量"""
        self._local_data_size = num_samples

    def set_parameters(self, parameters: list[list[float]]) -> None:
        """设置模型参数"""
        self._local_parameters = [layer.copy() for layer in parameters]

    def local_train(
        self,
        global_parameters: list[list[float]],
        epochs: int = 1,
        learning_rate: float = 0.01,
    ) -> ClientUpdate:
        """执行本地训练

        Args:
            global_parameters: 全局模型参数
            epochs: 本地训练轮数
            learning_rate: 学习率

        Returns:
            客户端更新
        """
        self.status = ClientStatus.TRAINING
        start_time = datetime.now()

        # 模拟本地训练: 参数 += 学习率 * 随机梯度
        trained_params: list[list[float]] = []
        for layer in global_parameters:
            gradient = [random.gauss(0, 0.1) for _ in layer]
            trained = [p + learning_rate * g for p, g in zip(layer, gradient)]
            trained_params.append(trained)

        # 差分隐私处理
        if self.dp_engine:
            dp_params: list[list[float]] = []
            for layer in trained_params:
                arr = np.array(layer)
                noisy = self.dp_engine.add_noise(arr)
                dp_params.append(noisy.tolist())
            trained_params = dp_params

        self._local_parameters = trained_params
        self.status = ClientStatus.COMPLETED

        duration = (datetime.now() - start_time).total_seconds() * 1000
        loss = random.uniform(0.1, 2.0)  # 模拟损失

        return ClientUpdate(
            client_id=self.client_id,
            round_id=0,
            parameters=trained_params,
            num_samples=self._local_data_size,
            loss=loss,
            duration_ms=duration,
            status=ClientStatus.COMPLETED,
        )


# ---------------------------------------------------------------------------
# 联邦聚合器
# ---------------------------------------------------------------------------

class FederatedAggregator:
    """联邦聚合器

    支持 FedAvg 和加权平均聚合策略。
    """

    def __init__(self, strategy: AggregationStrategy = AggregationStrategy.FEDERATED_AVERAGING):
        self.strategy = strategy

    def aggregate(
        self,
        updates: list[ClientUpdate],
    ) -> list[list[float]]:
        """聚合客户端更新

        Args:
            updates: 客户端更新列表

        Returns:
            聚合后的全局参数
        """
        if not updates:
            raise ValueError("没有客户端更新可聚合")

        if self.strategy == AggregationStrategy.FEDERATED_AVERAGING:
            return self._federated_average(updates)
        elif self.strategy == AggregationStrategy.WEIGHTED_AVERAGING:
            return self._weighted_average(updates)
        else:
            raise ValueError(f"不支持的聚合策略: {self.strategy}")

    def _federated_average(self, updates: list[ClientUpdate]) -> list[list[float]]:
        """FedAvg: 简单平均"""
        num_updates = len(updates)
        first_params = updates[0].parameters
        num_layers = len(first_params)

        aggregated: list[list[float]] = []
        for layer_idx in range(num_layers):
            layer_len = len(first_params[layer_idx])
            avg_layer = [0.0] * layer_len
            for update in updates:
                for i in range(layer_len):
                    avg_layer[i] += update.parameters[layer_idx][i]
            avg_layer = [v / num_updates for v in avg_layer]
            aggregated.append(avg_layer)

        return aggregated

    def _weighted_average(self, updates: list[ClientUpdate]) -> list[list[float]]:
        """加权平均: 按数据量加权"""
        total_samples = sum(u.num_samples for u in updates)
        if total_samples == 0:
            return self._federated_average(updates)

        weights = [u.num_samples / total_samples for u in updates]
        first_params = updates[0].parameters
        num_layers = len(first_params)

        aggregated: list[list[float]] = []
        for layer_idx in range(num_layers):
            layer_len = len(first_params[layer_idx])
            weighted_layer = [0.0] * layer_len
            for update, weight in zip(updates, weights):
                for i in range(layer_len):
                    weighted_layer[i] += update.parameters[layer_idx][i] * weight
            aggregated.append(weighted_layer)

        return aggregated


# ---------------------------------------------------------------------------
# 联邦学习引擎
# ---------------------------------------------------------------------------

class FederatedLearningEngine:
    """联邦学习引擎

    协调多方参与的联邦训练流程, 集成差分隐私保护。

    用法:
        engine = FederatedLearningEngine(num_rounds=10)
        engine.add_client("client-1", num_samples=1000)
        session = await engine.run_training()
    """

    def __init__(
        self,
        num_rounds: int = 10,
        aggregation_strategy: AggregationStrategy = AggregationStrategy.FEDERATED_AVERAGING,
        privacy_params: DifferentialPrivacyParams | None = None,
        client_fraction: float = 1.0,
    ):
        self.num_rounds = num_rounds
        self.client_fraction = client_fraction
        self._clients: dict[str, FederatedClient] = {}
        self._aggregator = FederatedAggregator(aggregation_strategy)
        self._privacy_params = privacy_params or DifferentialPrivacyParams()
        self._sessions: dict[str, FederatedSession] = {}

    def add_client(
        self,
        client_id: str,
        num_samples: int = 0,
    ) -> FederatedClient:
        """添加联邦学习客户端

        Args:
            client_id: 客户端 ID
            num_samples: 本地数据量

        Returns:
            客户端实例
        """
        dp_engine = DifferentialPrivacyEngine(
            epsilon=self._privacy_params.epsilon,
            delta=self._privacy_params.delta,
            mechanism=self._privacy_params.mechanism,
            max_grad_norm=self._privacy_params.max_grad_norm,
            noise_multiplier=self._privacy_params.noise_multiplier,
        )
        client = FederatedClient(client_id=client_id, dp_engine=dp_engine)
        client.set_local_data(num_samples)
        self._clients[client_id] = client
        logger.info(f"添加联邦客户端: {client_id}, 数据量={num_samples}")
        return client

    def initialize_parameters(self, layer_sizes: list[int]) -> list[list[float]]:
        """初始化全局模型参数

        Args:
            layer_sizes: 各层大小

        Returns:
            初始化的参数
        """
        parameters = []
        for size in layer_sizes:
            layer = [random.gauss(0, 0.01) for _ in range(size)]
            parameters.append(layer)
        return parameters

    async def run_training(
        self,
        initial_parameters: list[list[float]] | None = None,
        layer_sizes: list[int] | None = None,
    ) -> FederatedSession:
        """运行联邦训练

        Args:
            initial_parameters: 初始模型参数
            layer_sizes: 各层大小 (若无 initial_parameters 则自动初始化)

        Returns:
            联邦训练会话
        """
        session = FederatedSession(
            num_clients=len(self._clients),
            num_rounds=self.num_rounds,
            aggregation_strategy=self._aggregator.strategy,
            privacy_params=self._privacy_params,
        )
        self._sessions[session.session_id] = session

        # 初始化参数
        if initial_parameters:
            global_params = initial_parameters
        elif layer_sizes:
            global_params = self.initialize_parameters(layer_sizes)
        else:
            global_params = [[0.0] * 10]

        session.global_parameters = global_params

        logger.info(
            f"联邦训练开始: session={session.session_id}, "
            f"clients={len(self._clients)}, rounds={self.num_rounds}"
        )

        for round_num in range(1, self.num_rounds + 1):
            session.current_round = round_num
            fed_round = FederatedRound(
                round_id=round_num,
                status=RoundStatus.COLLECTING,
                started_at=datetime.now(),
            )

            # 选择参与客户端
            selected_ids = self._select_clients()
            fed_round.selected_clients = selected_ids

            # 收集客户端更新
            updates: list[ClientUpdate] = []
            for client_id in selected_ids:
                client = self._clients[client_id]
                update = client.local_train(global_params)
                update.round_id = round_num
                updates.append(update)
                fed_round.client_updates.append(update)

            # 聚合
            fed_round.status = RoundStatus.AGGREGATING
            global_params = self._aggregator.aggregate(updates)
            session.global_parameters = global_params

            # 计算全局损失
            avg_loss = sum(u.loss for u in updates) / max(len(updates), 1)
            fed_round.global_loss = avg_loss
            fed_round.global_parameters = global_params
            fed_round.status = RoundStatus.COMPLETED
            fed_round.completed_at = datetime.now()

            session.rounds.append(fed_round)
            logger.info(f"联邦训练轮次 {round_num} 完成: loss={avg_loss:.4f}")

        session.completed_at = datetime.now()
        logger.info(f"联邦训练完成: session={session.session_id}")
        return session

    def _select_clients(self) -> list[str]:
        """选择参与本轮训练的客户端"""
        all_ids = list(self._clients.keys())
        num_selected = max(1, int(len(all_ids) * self.client_fraction))
        return random.sample(all_ids, min(num_selected, len(all_ids)))

    def get_session(self, session_id: str) -> FederatedSession | None:
        """获取训练会话"""
        return self._sessions.get(session_id)

    def list_sessions(self) -> list[FederatedSession]:
        """列出所有会话"""
        return list(self._sessions.values())
