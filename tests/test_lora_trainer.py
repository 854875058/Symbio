"""真 LoRA 训练后端测试。

纯逻辑测试（数据转换、依赖探测、stub 回退）无条件跑；真实训练测试需要
transformers+peft+torch 且能下载/加载极小模型，不满足时跳过，保证 CI 全绿。
"""

from __future__ import annotations

import json
import os

import pytest

from symbio.evolution.lora_trainer import (
    _sample_to_text,
    load_texts_from_jsonl,
    ensure_training_deps,
    TrainingDependencyError,
)

# 真训练依赖是否齐全
try:
    ensure_training_deps()
    _DEPS_READY = True
except TrainingDependencyError:
    _DEPS_READY = False

requires_training = pytest.mark.skipif(
    not _DEPS_READY, reason="training deps (torch/transformers/peft/datasets) missing"
)


# ---------- 数据转换（纯逻辑，无条件跑）----------

def test_sample_to_text_sharegpt():
    sample = {"conversations": [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好，有什么可以帮你"},
    ]}
    text = _sample_to_text(sample)
    assert "user: 你好" in text
    assert "assistant: 你好，有什么可以帮你" in text


def test_sample_to_text_sharegpt_from_value_keys():
    # sharegpt 也可能用 from/value 键
    sample = {"conversations": [
        {"from": "human", "value": "问题"},
        {"from": "gpt", "value": "回答"},
    ]}
    text = _sample_to_text(sample)
    assert "human: 问题" in text and "gpt: 回答" in text


def test_sample_to_text_openai():
    sample = {"messages": [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "hi"},
    ]}
    text = _sample_to_text(sample)
    assert "system: 你是助手" in text and "user: hi" in text


def test_sample_to_text_alpaca():
    sample = {"instruction": "翻译", "input": "hello", "output": "你好"}
    text = _sample_to_text(sample)
    assert "翻译" in text and "hello" in text and "assistant: 你好" in text


def test_load_texts_skips_blank_and_invalid(tmp_path):
    ds = tmp_path / "d.jsonl"
    ds.write_text(
        json.dumps({"conversations": [{"role": "user", "content": "a"}]}) + "\n"
        + "\n"                       # 空行
        + "not json\n"              # 非法 JSON
        + json.dumps({"messages": [{"role": "user", "content": "b"}]}) + "\n",
        encoding="utf-8",
    )
    texts = load_texts_from_jsonl(str(ds))
    assert len(texts) == 2


def test_load_texts_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        load_texts_from_jsonl("nonexistent-dataset.jsonl")


# ---------- stub 回退（无条件跑）----------

def test_stub_fallback_when_env_set(tmp_path, monkeypatch):
    """SYMBIO_FT_STUB=1 时应走 stub，不碰真训练。"""
    from symbio.evolution.fine_tuner import OfflineFineTuner, FineTuneConfig, JobStatus

    monkeypatch.setenv("SYMBIO_FT_STUB", "1")
    ds = tmp_path / "train.jsonl"
    ds.write_text(json.dumps({"conversations": [
        {"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]}) + "\n", encoding="utf-8")

    tuner = OfflineFineTuner(base_output_dir=str(tmp_path / "ft"))
    cfg = FineTuneConfig(model_name="sshleifer/tiny-gpt2", dataset_path=str(ds),
                         output_dir=str(tmp_path / "out"), epochs=1)
    job = tuner.start_job(cfg)
    assert job.status == JobStatus.COMPLETED
    # stub 造的指标（模拟 loss），且不应标 backend=lora
    assert job.metadata.get("backend") != "lora"
    assert len(job.metrics) > 0


# ---------- 真训练（需依赖 + 能加载极小模型）----------

@requires_training
def test_real_lora_training_produces_adapter_and_real_loss(tmp_path):
    """真跑一次极小 LoRA 训练：产出 adapter 权重 + 真实 loss + 进度回调。"""
    from symbio.evolution.lora_trainer import train_lora

    ds = tmp_path / "train.jsonl"
    with ds.open("w", encoding="utf-8") as f:
        for i in range(4):
            f.write(json.dumps({"conversations": [
                {"role": "user", "content": f"问题{i}"},
                {"role": "assistant", "content": "这是一个回答。"},
            ]}, ensure_ascii=False) + "\n")

    seen: list[dict] = []
    try:
        result = train_lora(
            base_model="sshleifer/tiny-gpt2",
            dataset_path=str(ds),
            output_dir=str(tmp_path / "out"),
            epochs=2, batch_size=2, max_seq_length=32, lora_rank=4,
            on_step=lambda r: seen.append(r),
        )
    except Exception as exc:
        # 极小模型需联网下载；网络不可用则跳过（非逻辑错误）
        msg = str(exc).lower()
        if any(k in msg for k in ("connection", "download", "http", "offline", "resolve", "timeout")):
            pytest.skip(f"tiny model unavailable offline: {exc}")
        raise

    # 真实产物
    assert any("adapter_model" in f for f in result.adapter_files)
    assert result.trainable_params > 0
    assert result.trainable_params < result.total_params  # LoRA 只训练一小部分
    # 真实 loss：是有限浮点数，且回调确实被触发
    assert result.final_loss is not None
    assert len(result.steps) > 0
    assert len(seen) == len(result.steps)
    for step in result.steps:
        assert isinstance(step["loss"], float)


@requires_training
def test_real_training_empty_dataset_raises(tmp_path):
    from symbio.evolution.lora_trainer import train_lora

    ds = tmp_path / "empty.jsonl"
    ds.write_text("\n\n", encoding="utf-8")  # 全空行
    with pytest.raises(ValueError):
        train_lora(base_model="sshleifer/tiny-gpt2", dataset_path=str(ds),
                   output_dir=str(tmp_path / "out"))
