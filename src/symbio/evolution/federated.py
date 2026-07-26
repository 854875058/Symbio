"""联邦学习 + 差分隐私：多客户端各自本地训 LoRA，只交换 adapter 权重做 FedAvg 聚合。

兑现能力账本里 federated_privacy 的承诺，建立在已做实的 LoRA 训练（lora_trainer.py）
之上。核心思想——**数据不动，权重动**：

  - 每个客户端用自己的本地数据集训一个 LoRA adapter（数据从不离开本地目录）
  - 只把 adapter 的权重张量（adapter_model.safetensors）交给聚合器
  - 聚合器按各客户端样本量做加权平均（FedAvg: θ = Σ nᵢ·θᵢ / Σ nᵢ）
  - 可选差分隐私：对上传的权重先按 L2 范数裁剪，再加高斯噪声（DP-SGD 风格），
    让单个客户端的私有数据无法从权重反推

工程实现要点：FedAvg 直接在 safetensors 张量字典上做纯张量运算，不需要加载基座
模型——快、可离线、本机就能真验证聚合数学是否正确。

诚实边界：单机多客户端（各自独立目录）已可端到端验证；跨机器的安全传输、抗梯度
泄露攻击、拜占庭鲁棒聚合等生产级联邦特性尚未实现。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from symbio.utils.logger import get_logger

logger = get_logger("federated")


class FederatedError(RuntimeError):
    """联邦学习相关错误（依赖缺失 / adapter 不匹配 / 无客户端等）。"""


class PrivacyBudgetExhausted(FederatedError):
    """隐私预算已耗尽 —— 再加噪也无法提供承诺的 (ε, δ) 保证。"""


# ---------------------------------------------------------------------------
# safetensors 张量读写（FedAvg 直接在权重字典上算，不碰基座模型）
# ---------------------------------------------------------------------------


def _find_adapter_weights(adapter_dir: str) -> Path:
    """定位一个 adapter 目录里的权重文件（peft 存为 adapter_model.safetensors）。"""
    d = Path(adapter_dir)
    cand = d / "adapter_model.safetensors"
    if cand.is_file():
        return cand
    # 兼容极少数 .bin 存法
    alt = d / "adapter_model.bin"
    if alt.is_file():
        return alt
    matches = list(d.glob("adapter_model.*"))
    if matches:
        return matches[0]
    raise FederatedError(f"adapter 目录内未找到权重文件: {adapter_dir}")


def _load_state_dict(adapter_dir: str) -> dict[str, Any]:
    """把一个 adapter 的权重读成 {name: torch.Tensor} 字典。"""
    import torch  # noqa: F401  —— 供 .bin 分支使用

    path = _find_adapter_weights(adapter_dir)
    if path.suffix == ".safetensors":
        from safetensors.torch import load_file

        return load_file(str(path))
    return torch.load(str(path), map_location="cpu")


def _save_state_dict(state_dict: dict[str, Any], out_dir: str, template_dir: str) -> list[str]:
    """把聚合后的权重字典写成一个新的 adapter 目录（复用模板的 adapter_config.json）。"""
    from safetensors.torch import save_file

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    weights_path = out / "adapter_model.safetensors"
    save_file(state_dict, str(weights_path))

    written = [str(weights_path)]
    # 复用模板 adapter 的配置（结构相同，聚合不改结构）
    tmpl_cfg = Path(template_dir) / "adapter_config.json"
    if tmpl_cfg.is_file():
        cfg_out = out / "adapter_config.json"
        cfg_out.write_text(tmpl_cfg.read_text(encoding="utf-8"), encoding="utf-8")
        written.append(str(cfg_out))
    return written


# ---------------------------------------------------------------------------
# 差分隐私：L2 裁剪 + 高斯噪声（DP-SGD 风格）
# ---------------------------------------------------------------------------


def apply_dp_noise(
    state_dict: dict[str, Any],
    clip_norm: float = 1.0,
    noise_multiplier: float = 0.0,
    generator: Optional[Any] = None,
) -> dict[str, Any]:
    """对权重字典做差分隐私处理：先按全局 L2 范数裁剪，再加高斯噪声。

    - clip_norm：把整份权重的 L2 范数裁到不超过 clip_norm（限制单客户端影响，
      是 DP 的敏感度上界）。
    - noise_multiplier：高斯噪声标准差 = noise_multiplier * clip_norm；为 0 时
      只裁剪不加噪（等价于关闭 DP 的随机部分，结果确定）。
    - generator：可选 torch.Generator，用于可复现测试。

    返回新字典，不改原字典。
    """
    import torch

    # 1) 计算全局 L2 范数（所有张量拼一起看）
    total_sq = 0.0
    for t in state_dict.values():
        if torch.is_floating_point(t):
            total_sq += float(torch.sum(t.double() * t.double()))
    global_norm = total_sq**0.5
    scale = 1.0
    if global_norm > clip_norm and global_norm > 0:
        scale = clip_norm / global_norm

    std = noise_multiplier * clip_norm
    out: dict[str, Any] = {}
    for name, t in state_dict.items():
        if not torch.is_floating_point(t):
            out[name] = t.clone()
            continue
        clipped = t * scale
        if std > 0:
            noise = torch.normal(
                mean=0.0,
                std=std,
                size=clipped.shape,
                generator=generator,
                dtype=clipped.dtype,
            )
            out[name] = clipped + noise
        else:
            out[name] = clipped
    return out


def state_dict_l2_norm(state_dict: dict[str, Any]) -> float:
    """全局 L2 范数（测试/诊断用）。"""
    import torch

    total_sq = 0.0
    for t in state_dict.values():
        if torch.is_floating_point(t):
            total_sq += float(torch.sum(t.double() * t.double()))
    return total_sq**0.5


# ---------------------------------------------------------------------------
# 隐私预算记账与审计
# ---------------------------------------------------------------------------


@dataclass
class PrivacyAuditRecord:
    """一次 DP 处理的审计记录 —— 「加过噪」这件事本身必须可追溯、可复核。"""

    round_id: int
    epsilon_spent: float
    delta: float
    clip_norm: float
    noise_multiplier: float
    noise_std: float
    norm_before: float
    norm_after: float
    client_id: str = ""
    record_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "client_id": self.client_id,
            "round_id": self.round_id,
            "epsilon_spent": self.epsilon_spent,
            "delta": self.delta,
            "clip_norm": self.clip_norm,
            "noise_multiplier": self.noise_multiplier,
            "noise_std": self.noise_std,
            "norm_before": self.norm_before,
            "norm_after": self.norm_after,
            "timestamp": self.timestamp.isoformat(),
        }


class PrivacyAccountant:
    """差分隐私预算记账器。

    ``apply_dp_noise`` 只做裁剪+加噪这一步的数学，本身不知道「一共花了多少隐私」。
    但 DP 的保证是按累计预算算的：同一份数据被反复加噪上传，ε 会线性累加，
    超出预算后所谓的隐私保证就不成立了。这个记账器负责：

      - 把每轮花掉的 ε 累加起来，预算耗尽时直接拒绝（而不是继续假装安全）
      - 按高斯机制换算噪声系数：σ = √(2·ln(1.25/δ)) / ε
      - 留下审计记录，事后能证明每一轮到底加了多少噪

    诚实边界：这里用的是最保守的基础组合定理（ε 线性累加），没有实现
    RDP/moments accountant 那样更紧的组合界，因此报出的预算消耗偏悲观。
    """

    def __init__(
        self,
        *,
        epsilon: float = 1.0,
        delta: float = 1e-5,
        clip_norm: float = 1.0,
    ):
        if epsilon <= 0:
            raise ValueError("epsilon 必须为正")
        if not 0 < delta < 1:
            raise ValueError("delta 必须在 (0, 1) 区间内")
        self.epsilon = epsilon
        self.delta = delta
        self.clip_norm = clip_norm
        self._spent = 0.0
        self._records: list[PrivacyAuditRecord] = []

    @property
    def total_spent(self) -> float:
        """已消耗的隐私预算。"""
        return self._spent

    @property
    def remaining_budget(self) -> float:
        """剩余隐私预算。"""
        return max(0.0, self.epsilon - self._spent)

    def noise_multiplier_for(self, epsilon_per_round: float) -> float:
        """高斯机制噪声系数：σ/clip = √(2·ln(1.25/δ)) / ε。"""
        if epsilon_per_round <= 0:
            raise ValueError("每轮 epsilon 必须为正")
        return math.sqrt(2 * math.log(1.25 / self.delta)) / epsilon_per_round

    def spend(self, epsilon_per_round: float) -> float:
        """扣减预算，返回本轮应使用的噪声系数。预算不足时拒绝。"""
        if epsilon_per_round <= 0:
            raise ValueError("每轮 epsilon 必须为正")
        if epsilon_per_round > self.remaining_budget + 1e-12:
            raise PrivacyBudgetExhausted(
                f"隐私预算不足: 需要 ε={epsilon_per_round}，剩余 ε={self.remaining_budget:.4f}"
                f"（总预算 {self.epsilon}，已用 {self._spent:.4f}）"
            )
        self._spent += epsilon_per_round
        return self.noise_multiplier_for(epsilon_per_round)

    def privatize(
        self,
        state_dict: dict[str, Any],
        *,
        epsilon_per_round: float,
        round_id: int = 0,
        client_id: str = "",
        generator: Optional[Any] = None,
    ) -> tuple[dict[str, Any], PrivacyAuditRecord]:
        """扣预算 → 裁剪+加噪 → 记审计。返回处理后的权重和审计记录。"""
        noise_multiplier = self.spend(epsilon_per_round)
        norm_before = state_dict_l2_norm(state_dict)
        noised = apply_dp_noise(
            state_dict,
            clip_norm=self.clip_norm,
            noise_multiplier=noise_multiplier,
            generator=generator,
        )
        record = PrivacyAuditRecord(
            client_id=client_id,
            round_id=round_id,
            epsilon_spent=epsilon_per_round,
            delta=self.delta,
            clip_norm=self.clip_norm,
            noise_multiplier=noise_multiplier,
            noise_std=noise_multiplier * self.clip_norm,
            norm_before=norm_before,
            norm_after=state_dict_l2_norm(noised),
        )
        self._records.append(record)
        logger.info(
            f"[DP] client={client_id or '-'} round={round_id} "
            f"ε={epsilon_per_round} σ={record.noise_std:.4f} "
            f"norm {norm_before:.4f} → {record.norm_after:.4f} "
            f"（剩余预算 ε={self.remaining_budget:.4f}）"
        )
        return noised, record

    def get_audit_records(self) -> list[PrivacyAuditRecord]:
        """获取全部审计记录。"""
        return list(self._records)

    def audit_report(self) -> dict[str, Any]:
        """汇总审计报告。"""
        return {
            "epsilon_budget": self.epsilon,
            "epsilon_spent": self._spent,
            "epsilon_remaining": self.remaining_budget,
            "delta": self.delta,
            "clip_norm": self.clip_norm,
            "num_records": len(self._records),
            "records": [r.to_dict() for r in self._records],
        }


# ---------------------------------------------------------------------------
# FedAvg 聚合：按样本量加权平均各客户端 adapter 权重
# ---------------------------------------------------------------------------


def fedavg_state_dicts(
    state_dicts: list[dict[str, Any]],
    weights: list[float],
) -> dict[str, Any]:
    """FedAvg 核心：对齐 key 的张量按权重加权平均 θ = Σ wᵢ·θᵢ / Σ wᵢ。

    纯张量运算，是联邦聚合的数学本体，可脱离训练单独验证。
    """
    import torch

    if not state_dicts:
        raise FederatedError("没有可聚合的客户端权重")
    if len(state_dicts) != len(weights):
        raise FederatedError("state_dicts 与 weights 数量不一致")
    total_w = float(sum(weights))
    if total_w <= 0:
        raise FederatedError("权重之和必须为正")

    keys = set(state_dicts[0].keys())
    for sd in state_dicts[1:]:
        if set(sd.keys()) != keys:
            raise FederatedError("客户端 adapter 权重的 key 不一致，无法聚合")

    agg: dict[str, Any] = {}
    for k in state_dicts[0].keys():
        acc = None
        for sd, w in zip(state_dicts, weights):
            t = sd[k]
            if not torch.is_floating_point(t):
                # 非浮点（如量化 scale）取第一个客户端的，不做平均
                acc = t.clone()
                break
            contrib = t.double() * (w / total_w)
            acc = contrib if acc is None else acc + contrib
        # 还原回原 dtype
        agg[k] = (
            acc.to(state_dicts[0][k].dtype) if torch.is_floating_point(state_dicts[0][k]) else acc
        )
    return agg


def fedavg_aggregate(
    client_adapters: list[str],
    weights: Optional[list[float]] = None,
    output_dir: str = "",
    dp_clip_norm: float = 0.0,
    dp_noise_multiplier: float = 0.0,
) -> dict[str, Any]:
    """读多个客户端 adapter → （可选 DP）→ FedAvg 聚合 → 写全局 adapter。

    Args:
        client_adapters: 各客户端 adapter 目录路径。
        weights: 各客户端权重（通常是本地样本数）；None 则等权。
        output_dir: 全局 adapter 输出目录。
        dp_clip_norm: >0 时对每个客户端权重先做 DP 裁剪+噪声再聚合。
        dp_noise_multiplier: DP 高斯噪声系数。

    Returns:
        聚合报告（客户端数、权重、输出文件、聚合后 L2 范数）。
    """
    if not client_adapters:
        raise FederatedError("没有客户端 adapter 可聚合")
    if weights is None:
        weights = [1.0] * len(client_adapters)

    state_dicts = []
    for adir in client_adapters:
        sd = _load_state_dict(adir)
        if dp_clip_norm > 0:
            sd = apply_dp_noise(sd, clip_norm=dp_clip_norm, noise_multiplier=dp_noise_multiplier)
        state_dicts.append(sd)

    agg = fedavg_state_dicts(state_dicts, weights)

    written = []
    if output_dir:
        written = _save_state_dict(agg, output_dir, template_dir=client_adapters[0])

    report = {
        "num_clients": len(client_adapters),
        "weights": list(weights),
        "dp_applied": dp_clip_norm > 0,
        "output_dir": output_dir,
        "output_files": written,
        "aggregated_l2_norm": state_dict_l2_norm(agg),
        "num_tensors": len(agg),
    }
    logger.info(
        f"[FedAvg] 聚合完成: clients={report['num_clients']}, "
        f"dp={report['dp_applied']}, tensors={report['num_tensors']}"
    )
    return report


# ---------------------------------------------------------------------------
# 联邦客户端 / 协调器
# ---------------------------------------------------------------------------


@dataclass
class FederatedClient:
    """一个联邦客户端：持有本地数据集，本地训练出 adapter（数据不外传）。"""

    client_id: str
    dataset_path: str  # 本地数据集，只在本地读
    num_samples: int = 0  # 本地样本数，作为 FedAvg 权重
    adapter_dir: str = ""  # 本地训练产出的 adapter 目录

    def train_local(
        self,
        *,
        base_model: str,
        output_root: str,
        epochs: int = 1,
        lora_rank: int = 8,
        **train_kwargs: Any,
    ) -> "FederatedClient":
        """在本地跑一次 LoRA 训练，产出本地 adapter。数据从不离开 dataset_path。"""
        from symbio.evolution.lora_trainer import load_texts_from_jsonl, train_lora

        # 本地样本数（作为聚合权重）
        if not self.num_samples:
            try:
                self.num_samples = len(load_texts_from_jsonl(self.dataset_path))
            except Exception:
                self.num_samples = 0

        out_dir = str(Path(output_root) / f"client_{self.client_id}")
        result = train_lora(
            base_model=base_model,
            dataset_path=self.dataset_path,
            output_dir=out_dir,
            epochs=epochs,
            lora_rank=lora_rank,
            **train_kwargs,
        )
        # train_lora 把 adapter 落在 <out_dir>/adapter
        self.adapter_dir = str(Path(result.output_dir) / "adapter")
        logger.info(
            f"[FL] 客户端 {self.client_id} 本地训练完成: "
            f"samples={self.num_samples}, adapter={self.adapter_dir}"
        )
        return self


class FederatedCoordinator:
    """编排一轮联邦学习：各客户端本地训练 → （可选 DP）→ FedAvg 聚合。"""

    def __init__(
        self,
        clients: list[FederatedClient],
        accountant: Optional[PrivacyAccountant] = None,
    ):
        if not clients:
            raise FederatedError("至少需要一个联邦客户端")
        self.clients = clients
        # 可选：带预算记账的 DP。传了 accountant 就按预算走，超支直接拒绝聚合。
        self.accountant = accountant
        self._round_id = 0

    def run_round(
        self,
        *,
        base_model: str,
        output_root: str,
        epochs: int = 1,
        lora_rank: int = 8,
        dp_clip_norm: float = 0.0,
        dp_noise_multiplier: float = 0.0,
        dp_epsilon_per_round: float = 0.0,
        **train_kwargs: Any,
    ) -> dict[str, Any]:
        """跑一整轮联邦：训练所有客户端并聚合出全局 adapter。

        DP 有两种用法，二选一：
          - 直接给 dp_clip_norm / dp_noise_multiplier：一次性加噪，不记账。
          - 构造时传 accountant 并给 dp_epsilon_per_round：按 (ε, δ) 预算换算噪声、
            扣减预算、留审计记录；预算耗尽时抛 PrivacyBudgetExhausted。
        """
        self._round_id += 1
        for client in self.clients:
            if not client.adapter_dir:
                client.train_local(
                    base_model=base_model,
                    output_root=output_root,
                    epochs=epochs,
                    lora_rank=lora_rank,
                    **train_kwargs,
                )

        adapters = [c.adapter_dir for c in self.clients]
        weights = [float(c.num_samples or 1) for c in self.clients]
        global_dir = str(Path(output_root) / "global_adapter")

        if self.accountant is not None and dp_epsilon_per_round > 0:
            # 逐客户端扣预算加噪，再对已加噪的权重做 FedAvg
            state_dicts = []
            for client in self.clients:
                sd = _load_state_dict(client.adapter_dir)
                sd, _ = self.accountant.privatize(
                    sd,
                    epsilon_per_round=dp_epsilon_per_round,
                    round_id=self._round_id,
                    client_id=client.client_id,
                )
                state_dicts.append(sd)
            agg = fedavg_state_dicts(state_dicts, weights)
            written = _save_state_dict(agg, global_dir, template_dir=adapters[0])
            report: dict[str, Any] = {
                "num_clients": len(adapters),
                "weights": list(weights),
                "dp_applied": True,
                "output_dir": global_dir,
                "output_files": written,
                "aggregated_l2_norm": state_dict_l2_norm(agg),
                "num_tensors": len(agg),
                "privacy": self.accountant.audit_report(),
            }
        else:
            report = fedavg_aggregate(
                adapters,
                weights=weights,
                output_dir=global_dir,
                dp_clip_norm=dp_clip_norm,
                dp_noise_multiplier=dp_noise_multiplier,
            )

        report["round_id"] = self._round_id
        report["clients"] = [
            {"client_id": c.client_id, "num_samples": c.num_samples, "adapter_dir": c.adapter_dir}
            for c in self.clients
        ]
        report["global_adapter_dir"] = global_dir
        return report
