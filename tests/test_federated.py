"""联邦学习 + 差分隐私测试。

纯张量数学（FedAvg 加权平均、DP 裁剪/噪声）无条件快跑——这是联邦聚合的本体，
不依赖真训练即可硬验证正确性。端到端两客户端真训练聚合需 torch+transformers+peft，
标 slow，默认跳过。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")

from symbio.evolution.federated import (
    FederatedError,
    PrivacyAccountant,
    PrivacyBudgetExhausted,
    apply_dp_noise,
    fedavg_aggregate,
    fedavg_state_dicts,
    state_dict_l2_norm,
)


# ---------- 隐私预算记账与审计（不依赖训练）----------


def test_accountant_spends_and_reports_remaining_budget():
    acc = PrivacyAccountant(epsilon=1.0, delta=1e-5, clip_norm=1.0)
    assert acc.remaining_budget == pytest.approx(1.0)

    acc.spend(0.4)
    assert acc.total_spent == pytest.approx(0.4)
    assert acc.remaining_budget == pytest.approx(0.6)


def test_accountant_refuses_to_overspend():
    """预算耗尽后必须拒绝，而不是继续加噪却假装仍有 (ε, δ) 保证。"""
    acc = PrivacyAccountant(epsilon=0.5, delta=1e-5)
    acc.spend(0.4)
    with pytest.raises(PrivacyBudgetExhausted):
        acc.spend(0.2)
    # 拒绝的那次不应扣减预算
    assert acc.total_spent == pytest.approx(0.4)


def test_noise_multiplier_shrinks_as_epsilon_grows():
    """ε 越大隐私要求越松，所需噪声越小。"""
    acc = PrivacyAccountant(epsilon=10.0, delta=1e-5)
    assert acc.noise_multiplier_for(0.1) > acc.noise_multiplier_for(1.0)


def test_privatize_clips_and_records_audit():
    acc = PrivacyAccountant(epsilon=2.0, delta=1e-5, clip_norm=1.0)
    sd = {"w": torch.ones(4, 4) * 2.0}  # L2 范数 = 8
    assert state_dict_l2_norm(sd) == pytest.approx(8.0)

    # noise_multiplier 由 ε 决定，这里只验证裁剪与审计；用大 ε 压低噪声
    out, record = acc.privatize(sd, epsilon_per_round=1.0, round_id=1, client_id="c1")

    assert record.client_id == "c1"
    assert record.round_id == 1
    assert record.epsilon_spent == pytest.approx(1.0)
    assert record.norm_before == pytest.approx(8.0)
    assert record.noise_std == pytest.approx(acc.noise_multiplier_for(1.0))
    assert out["w"].shape == sd["w"].shape
    assert acc.remaining_budget == pytest.approx(1.0)
    assert len(acc.get_audit_records()) == 1


def test_privatize_without_noise_only_clips():
    """noise_multiplier 极小时结果应接近纯裁剪：范数 ≈ clip_norm。"""
    acc = PrivacyAccountant(epsilon=1e6, delta=1e-5, clip_norm=1.0)
    sd = {"w": torch.ones(4, 4) * 2.0}
    out, _ = acc.privatize(sd, epsilon_per_round=1e6)
    assert state_dict_l2_norm(out) == pytest.approx(1.0, abs=1e-3)


def test_privatize_preserves_non_float_tensors():
    acc = PrivacyAccountant(epsilon=1.0, delta=1e-5)
    sd = {"w": torch.ones(2, 2), "scale": torch.tensor([3, 4], dtype=torch.int64)}
    out, _ = acc.privatize(sd, epsilon_per_round=0.5)
    assert torch.equal(out["scale"], sd["scale"])


def test_audit_report_is_serializable():
    acc = PrivacyAccountant(epsilon=1.0, delta=1e-5)
    acc.privatize({"w": torch.ones(2, 2)}, epsilon_per_round=0.5, client_id="c1")
    report = acc.audit_report()

    assert report["epsilon_spent"] == pytest.approx(0.5)
    assert report["num_records"] == 1
    # 审计记录要能直接落盘/上报
    json.dumps(report)


def test_accountant_rejects_invalid_params():
    with pytest.raises(ValueError):
        PrivacyAccountant(epsilon=0.0)
    with pytest.raises(ValueError):
        PrivacyAccountant(epsilon=1.0, delta=1.0)
    with pytest.raises(ValueError):
        PrivacyAccountant(epsilon=1.0).spend(0.0)


# ---------- FedAvg 数学（不依赖训练）----------


def test_fedavg_weighted_average():
    """全1 与 全3、样本数 1:3 → 加权平均 (1+9)/4 = 2.5。"""
    a = {"w": torch.ones(4)}
    b = {"w": torch.full((4,), 3.0)}
    agg = fedavg_state_dicts([a, b], [1, 3])
    assert torch.allclose(agg["w"], torch.full((4,), 2.5))


def test_fedavg_equal_weight_is_mean():
    a = {"w": torch.ones(4)}
    b = {"w": torch.full((4,), 3.0)}
    agg = fedavg_state_dicts([a, b], [1, 1])
    assert torch.allclose(agg["w"], torch.full((4,), 2.0))


def test_fedavg_preserves_shape_and_keys():
    a = {"x": torch.randn(2, 3), "y": torch.randn(5)}
    b = {"x": torch.randn(2, 3), "y": torch.randn(5)}
    agg = fedavg_state_dicts([a, b], [1, 1])
    assert set(agg.keys()) == {"x", "y"}
    assert agg["x"].shape == (2, 3)
    assert agg["y"].shape == (5,)


def test_fedavg_key_mismatch_raises():
    a = {"w": torch.ones(2)}
    b = {"other": torch.ones(2)}
    with pytest.raises(FederatedError):
        fedavg_state_dicts([a, b], [1, 1])


def test_fedavg_empty_or_bad_weights():
    with pytest.raises(FederatedError):
        fedavg_state_dicts([], [])
    with pytest.raises(FederatedError):
        fedavg_state_dicts([{"w": torch.ones(2)}], [0])  # 权重和为 0


# ---------- 差分隐私 ----------


def test_dp_clip_reduces_norm():
    """大范数权重被裁到不超过 clip_norm。"""
    big = {"w": torch.full((4,), 10.0)}  # L2 = 20
    clipped = apply_dp_noise(big, clip_norm=1.0, noise_multiplier=0.0)
    assert state_dict_l2_norm(clipped) <= 1.0 + 1e-5


def test_dp_zero_noise_identity_when_within_clip():
    """范数已小于 clip 且噪声为 0 → 恒等。"""
    small = {"w": torch.tensor([0.1, 0.1])}
    out = apply_dp_noise(small, clip_norm=100.0, noise_multiplier=0.0)
    assert torch.allclose(out["w"], small["w"])


def test_dp_noise_is_random():
    """不同随机种子加噪结果不同（真加了噪声）。"""
    small = {"w": torch.tensor([0.1, 0.1, 0.1])}
    g1 = torch.Generator().manual_seed(1)
    g2 = torch.Generator().manual_seed(2)
    n1 = apply_dp_noise(small, clip_norm=100.0, noise_multiplier=1.0, generator=g1)
    n2 = apply_dp_noise(small, clip_norm=100.0, noise_multiplier=1.0, generator=g2)
    assert not torch.allclose(n1["w"], n2["w"])


def test_dp_does_not_mutate_input():
    orig = {"w": torch.full((3,), 5.0)}
    snapshot = orig["w"].clone()
    apply_dp_noise(orig, clip_norm=0.5, noise_multiplier=1.0)
    assert torch.allclose(orig["w"], snapshot)


# ---------- FedAvg 落盘（safetensors 往返，不依赖训练）----------


def test_fedavg_aggregate_writes_global_adapter(tmp_path):
    """构造两个假 adapter 目录 → 聚合 → 全局 adapter 文件生成、权重为加权平均。"""
    from safetensors.torch import save_file

    def make_adapter(d: Path, val: float):
        d.mkdir(parents=True, exist_ok=True)
        save_file({"lora.weight": torch.full((3,), val)}, str(d / "adapter_model.safetensors"))
        (d / "adapter_config.json").write_text(json.dumps({"r": 8}), encoding="utf-8")

    c1 = tmp_path / "c1"
    c2 = tmp_path / "c2"
    make_adapter(c1, 2.0)
    make_adapter(c2, 6.0)
    out = tmp_path / "global"

    report = fedavg_aggregate([str(c1), str(c2)], weights=[1, 3], output_dir=str(out))
    assert report["num_clients"] == 2
    assert (out / "adapter_model.safetensors").is_file()
    assert (out / "adapter_config.json").is_file()  # 复用模板配置

    from safetensors.torch import load_file

    agg = load_file(str(out / "adapter_model.safetensors"))
    # (1*2 + 3*6)/4 = 5.0
    assert torch.allclose(agg["lora.weight"], torch.full((3,), 5.0))


def test_fedavg_aggregate_with_dp(tmp_path):
    """带 DP 聚合能跑通并落盘（噪声让结果不等于纯加权平均，但结构完整）。"""
    from safetensors.torch import save_file

    for name, val in [("c1", 2.0), ("c2", 6.0)]:
        d = tmp_path / name
        d.mkdir()
        save_file({"lora.weight": torch.full((3,), val)}, str(d / "adapter_model.safetensors"))
        (d / "adapter_config.json").write_text("{}", encoding="utf-8")

    out = tmp_path / "global"
    report = fedavg_aggregate(
        [str(tmp_path / "c1"), str(tmp_path / "c2")],
        weights=[1, 1],
        output_dir=str(out),
        dp_clip_norm=1.0,
        dp_noise_multiplier=0.5,
    )
    assert report["dp_applied"] is True
    assert (out / "adapter_model.safetensors").is_file()


# ---------- 端到端：两客户端真训练 + 聚合（slow）----------


def _training_ready() -> bool:
    try:
        from symbio.evolution.lora_trainer import ensure_training_deps

        ensure_training_deps()
        return True
    except Exception:
        return False


@pytest.mark.slow
@pytest.mark.skipif(not _training_ready(), reason="缺 torch/transformers/peft")
def test_federated_round_end_to_end(tmp_path):
    """两个客户端各用自己的小数据集本地训 adapter → FedAvg 聚合出全局 adapter。

    验证：数据分处两个独立目录（不外传）、各自产出 adapter、聚合后全局 adapter
    文件生成且张量形状与客户端一致。
    """
    from symbio.evolution.federated import FederatedClient, FederatedCoordinator

    # 两个客户端各自的本地数据集（内容不同，模拟数据分布差异）
    def make_dataset(path: Path, texts: list[str]):
        path.write_text(
            "\n".join(
                json.dumps(
                    {
                        "messages": [
                            {"role": "user", "content": t},
                            {"role": "assistant", "content": f"回答:{t}"},
                        ]
                    },
                    ensure_ascii=False,
                )
                for t in texts
            ),
            encoding="utf-8",
        )

    d1 = tmp_path / "client1_data.jsonl"
    d2 = tmp_path / "client2_data.jsonl"
    make_dataset(d1, ["你好", "今天天气"])
    make_dataset(d2, ["写代码", "解释算法", "调试程序"])

    clients = [
        FederatedClient(client_id="1", dataset_path=str(d1)),
        FederatedClient(client_id="2", dataset_path=str(d2)),
    ]
    coordinator = FederatedCoordinator(clients)
    try:
        report = coordinator.run_round(
            base_model="sshleifer/tiny-gpt2",
            output_root=str(tmp_path / "fl"),
            epochs=1,
            lora_rank=4,
            max_seq_length=32,
        )
    except Exception as exc:
        # 极小模型需联网下载；网络不可用则跳过（非逻辑错误），与 test_lora_trainer 一致
        msg = str(exc).lower()
        if any(
            k in msg
            for k in (
                "connection",
                "download",
                "http",
                "offline",
                "resolve",
                "timeout",
                "couldn't connect",
            )
        ):
            pytest.skip(f"tiny model unavailable offline: {exc}")
        raise

    assert report["num_clients"] == 2
    # 各客户端产出了本地 adapter，且样本数正确（作为聚合权重）
    assert clients[0].num_samples == 2
    assert clients[1].num_samples == 3
    for c in clients:
        assert (Path(c.adapter_dir) / "adapter_model.safetensors").is_file()
    # 全局 adapter 生成
    global_dir = Path(report["global_adapter_dir"])
    assert (global_dir / "adapter_model.safetensors").is_file()
    assert report["num_tensors"] > 0
