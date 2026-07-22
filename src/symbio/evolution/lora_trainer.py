"""真 LoRA SFT 训练后端 —— 数据飞轮闭环的最后一环。

用 transformers + peft 在本地 GPU/CPU 上跑真正的 LoRA 微调：加载基座模型 →
套 LoRA adapter → 从 JSONL 数据集读样本 → transformers.Trainer 真训练 →
落盘 adapter 权重。真实 loss 通过 TrainerCallback 逐步回填。

与旧的 _run_training_stub（造假 loss）不同，这里跑的是真实反向传播。依赖缺失
（peft/transformers）或显式设 SYMBIO_FT_STUB=1 时，调用方应回退到 stub。

设计要点：
- 依赖探测集中在 ensure_training_deps()，缺啥报啥，不假装成功
- 数据格式兼容 DatasetExporter 产出的 sharegpt / alpaca / openai 三种 JSONL
- 训练进度经 callback 回调，宿主（fine_tuner）据此填 TrainingMetrics
- 小模型 + CPU 也能跑（测试用），有 GPU 自动用 GPU
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from symbio.utils.logger import get_logger

logger = get_logger("lora_trainer")


class TrainingDependencyError(RuntimeError):
    """真训练依赖缺失（transformers/peft/torch）。调用方应据此回退 stub。"""


def ensure_training_deps() -> None:
    """探测真训练所需依赖，缺失则抛 TrainingDependencyError（列出缺哪些）。"""
    missing: list[str] = []
    for mod in ("torch", "transformers", "peft", "datasets"):
        try:
            __import__(mod)
        except Exception:
            missing.append(mod)
    if missing:
        raise TrainingDependencyError(
            "真 LoRA 训练缺少依赖: "
            + ", ".join(missing)
            + "。安装: pip install torch transformers peft datasets"
        )


@dataclass
class LoraTrainResult:
    """真训练产出。"""

    output_dir: str
    steps: list[dict[str, Any]] = field(default_factory=list)  # 每步 {step,epoch,loss,lr}
    final_loss: Optional[float] = None
    adapter_files: list[str] = field(default_factory=list)
    base_model: str = ""
    trainable_params: int = 0
    total_params: int = 0


# ---------------------------------------------------------------------------
# 数据集：把 DatasetExporter 的 sharegpt/alpaca/openai JSONL 转成纯文本样本
# ---------------------------------------------------------------------------


def _sample_to_text(sample: dict[str, Any]) -> str:
    """把一条样本拍平成一段训练文本（role: content 逐行拼接）。

    兼容三种格式：
    - sharegpt: {"conversations":[{"role"/"from":..,"content"/"value":..}]}
    - openai:   {"messages":[{"role":..,"content":..}]}
    - alpaca:   {"instruction":..,"input":..,"output":..}
    """
    parts: list[str] = []
    if "conversations" in sample and isinstance(sample["conversations"], list):
        for turn in sample["conversations"]:
            role = turn.get("role") or turn.get("from") or "user"
            content = turn.get("content") or turn.get("value") or ""
            parts.append(f"{role}: {content}")
    elif "messages" in sample and isinstance(sample["messages"], list):
        for turn in sample["messages"]:
            parts.append(f"{turn.get('role', 'user')}: {turn.get('content', '')}")
    elif "instruction" in sample:
        instr = sample.get("instruction", "")
        inp = sample.get("input", "")
        out = sample.get("output", "")
        prompt = f"{instr}\n{inp}".strip()
        parts.append(f"user: {prompt}")
        parts.append(f"assistant: {out}")
    else:
        # 兜底：整条 JSON 当文本，避免丢样本
        parts.append(json.dumps(sample, ensure_ascii=False))
    return "\n".join(p for p in parts if p.strip())


def load_texts_from_jsonl(dataset_path: str) -> list[str]:
    """读 JSONL 数据集，返回训练文本列表（跳过空行/空样本）。"""
    path = Path(dataset_path)
    if not path.exists():
        raise FileNotFoundError(f"数据集不存在: {path}")
    texts: list[str] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                sample = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = _sample_to_text(sample)
            if text.strip():
                texts.append(text)
    return texts


# ---------------------------------------------------------------------------
# 真 LoRA 训练
# ---------------------------------------------------------------------------


def train_lora(
    *,
    base_model: str,
    dataset_path: str,
    output_dir: str,
    epochs: int = 1,
    learning_rate: float = 2e-4,
    batch_size: int = 1,
    max_seq_length: int = 512,
    lora_rank: int = 8,
    lora_alpha: int = 16,
    lora_dropout: float = 0.05,
    on_step: Optional[Callable[[dict[str, Any]], None]] = None,
) -> LoraTrainResult:
    """跑一次真 LoRA SFT，落盘 adapter，返回真实训练轨迹。

    on_step: 每次 Trainer 记录 loss 时回调 {step,epoch,loss,learning_rate}，
    宿主据此填 TrainingMetrics（实时进度）。
    """
    ensure_training_deps()

    import torch
    from transformers import (
        AutoModelForCausalLM,
        AutoTokenizer,
        Trainer,
        TrainingArguments,
        TrainerCallback,
    )
    from peft import LoraConfig, get_peft_model, TaskType
    from datasets import Dataset

    texts = load_texts_from_jsonl(dataset_path)
    if not texts:
        raise ValueError(f"数据集无有效样本: {dataset_path}")

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    logger.info(f"[LoRA] 加载基座模型: {base_model}（样本 {len(texts)} 条）")
    tokenizer = AutoTokenizer.from_pretrained(base_model)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token or tokenizer.unk_token

    model = AutoModelForCausalLM.from_pretrained(base_model)

    lora_cfg = LoraConfig(
        task_type=TaskType.CAUSAL_LM,
        r=lora_rank,
        lora_alpha=lora_alpha,
        lora_dropout=lora_dropout,
        bias="none",
    )
    model = get_peft_model(model, lora_cfg)
    trainable, total = _count_params(model)
    logger.info(f"[LoRA] 可训练参数 {trainable:,} / 总参数 {total:,}")

    def _tokenize(batch):
        enc = tokenizer(
            batch["text"],
            truncation=True,
            max_length=max_seq_length,
            padding="max_length",
        )
        enc["labels"] = [ids[:] for ids in enc["input_ids"]]
        return enc

    ds = Dataset.from_dict({"text": texts}).map(_tokenize, batched=True, remove_columns=["text"])

    steps: list[dict[str, Any]] = []

    class _StepCallback(TrainerCallback):
        def on_log(self, args, state, control, logs=None, **kwargs):
            if not logs or "loss" not in logs:
                return
            record = {
                "step": int(state.global_step),
                "epoch": int(state.epoch or 0),
                "loss": float(logs["loss"]),
                "learning_rate": float(logs.get("learning_rate", learning_rate)),
            }
            steps.append(record)
            if on_step:
                on_step(record)

    args = TrainingArguments(
        output_dir=str(out / "checkpoints"),
        num_train_epochs=epochs,
        per_device_train_batch_size=batch_size,
        learning_rate=learning_rate,
        logging_steps=1,
        save_strategy="no",
        report_to=[],
        disable_tqdm=True,
        use_cpu=not torch.cuda.is_available(),
    )
    trainer = Trainer(model=model, args=args, train_dataset=ds, callbacks=[_StepCallback()])
    logger.info(
        f"[LoRA] 开始训练 epochs={epochs} device={'cuda' if torch.cuda.is_available() else 'cpu'}"
    )
    trainer.train()

    # 落盘 adapter（真实 LoRA 权重）
    adapter_dir = out / "adapter"
    model.save_pretrained(str(adapter_dir))
    tokenizer.save_pretrained(str(adapter_dir))
    adapter_files = [str(p) for p in adapter_dir.glob("*") if p.is_file()]

    final_loss = steps[-1]["loss"] if steps else None
    logger.info(f"[LoRA] 训练完成 final_loss={final_loss} adapter={adapter_dir}")
    return LoraTrainResult(
        output_dir=str(out),
        steps=steps,
        final_loss=final_loss,
        adapter_files=adapter_files,
        base_model=base_model,
        trainable_params=trainable,
        total_params=total,
    )


def _count_params(model) -> tuple[int, int]:
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total = sum(p.numel() for p in model.parameters())
    return trainable, total
