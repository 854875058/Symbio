"""Offline Fine-Tuner — 离线微调基础设施。

提供从轨迹数据到微调作业的完整管线:
- 数据准备: 调用 DatasetExporter 将轨迹转换为标准微调格式
- 数据校验: 检查格式、数量、质量
- 训练管理: 提交/查询/管理微调作业（支持 Ray Train 或本地 stub）
- 指标追踪: 记录训练过程中的 loss、accuracy 等指标

当 use_ray=True 且 Ray 可用时使用 Ray Train；否则回退到本地训练桩。
实际训练循环为 stub，等待积累足够数据后接入真实训练。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from symbio.evolution.dataset_exporter import (
    DatasetExporter,
    ExportConfig,
    ExportFormat,
)
from symbio.utils.logger import get_logger

logger = get_logger("fine_tuner")


# =============================================================================
# 1. 枚举与数据模型
# =============================================================================


class JobStatus(str, Enum):
    """微调作业状态。"""

    PENDING = "pending"
    PREPARING = "preparing"
    TRAINING = "training"
    EVALUATING = "evaluating"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TrainingMetrics(BaseModel):
    """训练指标快照。"""

    step: int = Field(default=0, description="当前训练步数")
    epoch: int = Field(default=0, description="当前 epoch")
    loss: float = Field(default=0.0, description="训练损失")
    eval_loss: Optional[float] = Field(default=None, description="验证损失")
    accuracy: Optional[float] = Field(default=None, description="训练准确率")
    learning_rate: float = Field(default=0.0, description="当前学习率")
    tokens_per_second: Optional[float] = Field(
        default=None, description="每秒处理 token 数"
    )
    gpu_memory_mb: Optional[float] = Field(
        default=None, description="GPU 显存占用 (MB)"
    )
    logged_at: datetime = Field(
        default_factory=datetime.now, description="记录时间"
    )


class FineTuneConfig(BaseModel):
    """微调作业配置。"""

    model_name: str = Field(
        default="meta-llama/Llama-3-8B-Instruct",
        description="基础模型名称或路径",
    )
    dataset_path: str = Field(
        default="", description="微调数据集路径（JSONL 格式）"
    )
    output_dir: str = Field(
        default="./data/fine_tuned", description="模型输出目录"
    )
    epochs: int = Field(default=3, ge=1, le=100, description="训练轮数")
    learning_rate: float = Field(
        default=2e-5, gt=0, le=1.0, description="学习率"
    )
    batch_size: int = Field(
        default=4, ge=1, le=1024, description="每批样本数"
    )
    gradient_accumulation_steps: int = Field(
        default=1, ge=1, description="梯度累积步数"
    )
    max_seq_length: int = Field(
        default=4096, ge=64, le=131072, description="最大序列长度"
    )
    warmup_ratio: float = Field(
        default=0.05, ge=0.0, le=0.5, description="预热比例"
    )
    weight_decay: float = Field(
        default=0.01, ge=0.0, le=1.0, description="权重衰减"
    )
    use_ray: bool = Field(
        default=False, description="是否使用 Ray Train 分布式训练"
    )
    ray_address: Optional[str] = Field(
        default=None, description="Ray 集群地址，None 表示本地自动启动"
    )
    lora_rank: int = Field(default=8, ge=1, description="LoRA 秩")
    lora_alpha: int = Field(default=16, ge=1, description="LoRA alpha")
    lora_dropout: float = Field(
        default=0.05, ge=0.0, le=1.0, description="LoRA dropout"
    )
    use_lora: bool = Field(default=True, description="是否使用 LoRA 微调")


class ValidationReport(BaseModel):
    """数据集校验报告。"""

    is_valid: bool = Field(description="是否通过校验")
    sample_count: int = Field(default=0, description="样本总数")
    format_detected: Optional[str] = Field(
        default=None, description="检测到的数据格式"
    )
    errors: list[str] = Field(
        default_factory=list, description="致命错误（阻止训练）"
    )
    warnings: list[str] = Field(
        default_factory=list, description="警告（不影响训练但需关注）"
    )
    avg_conversation_turns: float = Field(
        default=0.0, description="平均对话轮次"
    )
    avg_content_length: float = Field(
        default=0.0, description="平均内容长度（字符）"
    )
    empty_samples: int = Field(default=0, description="空样本数")
    short_samples: int = Field(default=0, description="过短样本数（<10 字符）")


# =============================================================================
# 2. 微调作业模型
# =============================================================================


class FineTuneJob(BaseModel):
    """微调作业实例。"""

    job_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()), description="作业唯一 ID"
    )
    status: JobStatus = Field(
        default=JobStatus.PENDING, description="当前作业状态"
    )
    config: FineTuneConfig = Field(
        default_factory=FineTuneConfig, description="作业配置"
    )
    metrics: list[TrainingMetrics] = Field(
        default_factory=list, description="训练指标历史"
    )
    best_metrics: Optional[TrainingMetrics] = Field(
        default=None, description="最佳指标"
    )
    error_message: Optional[str] = Field(
        default=None, description="失败原因"
    )
    started_at: Optional[datetime] = Field(
        default=None, description="训练开始时间"
    )
    completed_at: Optional[datetime] = Field(
        default=None, description="训练结束时间"
    )
    created_at: datetime = Field(
        default_factory=datetime.now, description="作业创建时间"
    )
    output_path: Optional[str] = Field(
        default=None, description="产出模型路径"
    )

    @property
    def duration_seconds(self) -> Optional[float]:
        """训练耗时（秒）。"""
        if self.started_at is None:
            return None
        end = self.completed_at or datetime.now()
        return (end - self.started_at).total_seconds()

    @property
    def latest_metrics(self) -> Optional[TrainingMetrics]:
        """最新一条训练指标。"""
        return self.metrics[-1] if self.metrics else None

    @property
    def progress_ratio(self) -> float:
        """训练进度估算 (0-1)。"""
        if not self.metrics:
            return 0.0
        latest = self.metrics[-1]
        total_steps = self.config.epochs  # 粗略用 epoch 比例
        if total_steps <= 0:
            return 0.0
        return min(latest.epoch / total_steps, 1.0)


# =============================================================================
# 3. 离线微调器主类
# =============================================================================


class OfflineFineTuner:
    """离线微调器 — 从轨迹数据到微调作业的完整管线。

    核心能力:
    - 数据准备: 调用 DatasetExporter 转换轨迹为微调数据集
    - 数据校验: 验证格式、数量、质量
    - 作业管理: 提交、查询、取消微调作业
    - Ray 集成: 可选的 Ray Train 分布式训练支持

    Usage::

        tuner = OfflineFineTuner()

        # 准备数据集
        dataset_path = tuner.prepare_dataset("trajectories.jsonl", format="sharegpt")

        # 校验数据集
        report = tuner.validate_dataset(dataset_path)
        if report.is_valid:
            config = FineTuneConfig(dataset_path=dataset_path)
            job = tuner.start_job(config)
    """

    def __init__(self, base_output_dir: str = "./data/fine_tuning") -> None:
        self._base_output_dir = Path(base_output_dir)
        self._base_output_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, FineTuneJob] = {}

    # -------------------------------------------------------------------------
    # 数据准备
    # -------------------------------------------------------------------------

    def prepare_dataset(
        self,
        trajectory_path: str,
        format: str = "sharegpt",
        output_dir: Optional[str] = None,
    ) -> str:
        """将轨迹文件转换为微调数据集。

        调用 DatasetExporter 将轨迹 JSONL 文件导出为标准微调格式。

        Args:
            trajectory_path: 轨迹 JSONL 文件路径
            format: 目标格式 ("sharegpt" / "alpaca" / "openai")
            output_dir: 可选输出目录，默认使用 base_output_dir/datasets

        Returns:
            生成的微调数据集文件路径

        Raises:
            FileNotFoundError: 轨迹文件不存在
            ValueError: 不支持的格式
        """
        traj_path = Path(trajectory_path)
        if not traj_path.exists():
            raise FileNotFoundError(f"轨迹文件不存在: {traj_path}")

        fmt_map = {
            "sharegpt": ExportFormat.SHAREGPT,
            "alpaca": ExportFormat.ALPACA,
            "openai": ExportFormat.OPENAI,
        }
        export_format = fmt_map.get(format.lower())
        if export_format is None:
            raise ValueError(
                f"不支持的格式: {format}，可选: {list(fmt_map.keys())}"
            )

        out_dir = output_dir or str(self._base_output_dir / "datasets")
        config = ExportConfig(
            output_dir=out_dir,
            format=export_format,
            incremental=False,
            file_prefix="finetune",
        )
        exporter = DatasetExporter(config)

        logger.info(
            f"准备数据集: {traj_path} -> {format} 格式, 输出目录: {out_dir}"
        )
        report = exporter.export_from_jsonl(str(traj_path))

        if report.output_file is None:
            raise RuntimeError(
                f"数据集导出失败: 没有有效样本 (rejected={report.rejected}, "
                f"duplicates={report.duplicates})"
            )

        logger.info(
            f"数据集准备完成: {report.output_file} "
            f"(导出 {report.exported} 条, 拒绝 {report.rejected} 条)"
        )
        return report.output_file

    # -------------------------------------------------------------------------
    # 数据校验
    # -------------------------------------------------------------------------

    def validate_dataset(self, dataset_path: str) -> ValidationReport:
        """校验微调数据集的格式、数量和质量。

        Args:
            dataset_path: 数据集 JSONL 文件路径

        Returns:
            校验报告
        """
        ds_path = Path(dataset_path)
        errors: list[str] = []
        warnings: list[str] = []

        # 基本检查
        if not ds_path.exists():
            errors.append(f"数据集文件不存在: {ds_path}")
            return ValidationReport(is_valid=False, errors=errors)

        if ds_path.suffix.lower() != ".jsonl":
            warnings.append(f"文件扩展名非 .jsonl: {ds_path.suffix}")

        # 逐行解析
        samples: list[dict[str, Any]] = []
        parse_errors = 0
        format_candidates: dict[str, int] = {
            "sharegpt": 0,
            "alpaca": 0,
            "openai": 0,
        }

        with open(ds_path, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    samples.append(data)

                    # 检测格式
                    if "conversations" in data:
                        format_candidates["sharegpt"] += 1
                    elif "instruction" in data and "output" in data:
                        format_candidates["alpaca"] += 1
                    elif "messages" in data:
                        format_candidates["openai"] += 1

                except json.JSONDecodeError as exc:
                    parse_errors += 1
                    if parse_errors <= 5:
                        errors.append(f"第 {line_num} 行 JSON 解析失败: {exc}")

        if parse_errors > 5:
            errors.append(f"共 {parse_errors} 行解析失败（仅显示前 5 条）")

        # 检测主要格式
        detected_format = None
        if format_candidates:
            detected_format = max(format_candidates, key=format_candidates.get)
            if format_candidates[detected_format] == 0:
                detected_format = None

        # 内容质量检查
        empty_count = 0
        short_count = 0
        total_turns = 0
        total_length = 0

        for sample in samples:
            content = self._extract_sample_content(sample)
            total_length += len(content)

            if not content:
                empty_count += 1
            elif len(content) < 10:
                short_count += 1

            # 计算对话轮次
            turns = self._count_turns(sample)
            total_turns += turns

        sample_count = len(samples)
        avg_turns = total_turns / sample_count if sample_count > 0 else 0.0
        avg_length = total_length / sample_count if sample_count > 0 else 0.0

        # 错误/警告汇总
        if sample_count == 0:
            errors.append("数据集为空，没有任何有效样本")

        if sample_count > 0 and empty_count / sample_count > 0.1:
            warnings.append(
                f"空样本比例过高: {empty_count}/{sample_count} "
                f"({empty_count / sample_count:.1%})"
            )

        if sample_count > 0 and short_count / sample_count > 0.3:
            warnings.append(
                f"过短样本比例过高: {short_count}/{sample_count} "
                f"({short_count / sample_count:.1%})"
            )

        if sample_count < 100:
            warnings.append(
                f"样本数量较少 ({sample_count})，建议至少 100 条以获得较好效果"
            )

        if detected_format is None:
            warnings.append("未能识别数据集格式")

        is_valid = len(errors) == 0

        logger.info(
            f"数据集校验完成: valid={is_valid}, "
            f"samples={sample_count}, format={detected_format}, "
            f"errors={len(errors)}, warnings={len(warnings)}"
        )

        return ValidationReport(
            is_valid=is_valid,
            sample_count=sample_count,
            format_detected=detected_format,
            errors=errors,
            warnings=warnings,
            avg_conversation_turns=round(avg_turns, 2),
            avg_content_length=round(avg_length, 2),
            empty_samples=empty_count,
            short_samples=short_count,
        )

    @staticmethod
    def _extract_sample_content(sample: dict[str, Any]) -> str:
        """提取样本的主要文本内容。"""
        if "conversations" in sample:
            parts = []
            for msg in sample["conversations"]:
                content = msg.get("content", "")
                if content:
                    parts.append(content)
            return "\n".join(parts)
        if "messages" in sample:
            parts = []
            for msg in sample["messages"]:
                content = msg.get("content", "")
                if content:
                    parts.append(content)
            return "\n".join(parts)
        if "instruction" in sample:
            return sample.get("instruction", "") + sample.get("output", "")
        return ""

    @staticmethod
    def _count_turns(sample: dict[str, Any]) -> int:
        """统计样本对话轮次。"""
        if "conversations" in sample:
            return len(sample["conversations"])
        if "messages" in sample:
            return len(sample["messages"])
        if "instruction" in sample:
            return 2  # instruction + output
        return 0

    # -------------------------------------------------------------------------
    # 作业管理
    # -------------------------------------------------------------------------

    def start_job(self, config: FineTuneConfig) -> FineTuneJob:
        """提交一个微调作业。

        当 use_ray=True 且 Ray 可用时，尝试使用 Ray Train；
        否则回退到本地训练桩。

        Args:
            config: 微调配置

        Returns:
            创建的微调作业
        """
        job = FineTuneJob(config=config)
        job.status = JobStatus.PREPARING
        job.started_at = datetime.now()
        self._jobs[job.job_id] = job

        logger.info(
            f"提交微调作业: job_id={job.job_id}, "
            f"model={config.model_name}, "
            f"dataset={config.dataset_path}, "
            f"use_ray={config.use_ray}"
        )

        # 尝试启动训练
        try:
            if config.use_ray:
                self._start_ray_training(job)
            else:
                self._start_local_training(job)
        except Exception as exc:
            job.status = JobStatus.FAILED
            job.error_message = str(exc)
            job.completed_at = datetime.now()
            logger.error(f"作业 {job.job_id} 启动失败: {exc}")

        return job

    def get_job_status(self, job_id: str) -> FineTuneJob:
        """查询微调作业状态。

        Args:
            job_id: 作业 ID

        Returns:
            作业实例

        Raises:
            KeyError: 作业不存在
        """
        if job_id not in self._jobs:
            raise KeyError(f"作业不存在: {job_id}")
        return self._jobs[job_id]

    def list_jobs(
        self, status: Optional[JobStatus] = None
    ) -> list[FineTuneJob]:
        """列出所有微调作业。

        Args:
            status: 可选的状态过滤

        Returns:
            作业列表
        """
        jobs = list(self._jobs.values())
        if status is not None:
            jobs = [j for j in jobs if j.status == status]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    def cancel_job(self, job_id: str) -> FineTuneJob:
        """取消微调作业。

        Args:
            job_id: 作业 ID

        Returns:
            更新后的作业实例

        Raises:
            KeyError: 作业不存在
            ValueError: 作业已结束
        """
        job = self.get_job_status(job_id)
        terminal_states = {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
        if job.status in terminal_states:
            raise ValueError(
                f"作业 {job_id} 已处于终态 {job.status.value}，无法取消"
            )
        job.status = JobStatus.CANCELLED
        job.completed_at = datetime.now()
        logger.info(f"作业 {job_id} 已取消")
        return job

    # -------------------------------------------------------------------------
    # 训练后端
    # -------------------------------------------------------------------------

    def _start_ray_training(self, job: FineTuneJob) -> None:
        """尝试使用 Ray Train 启动分布式训练。

        如果 Ray 不可用，回退到本地训练桩。
        """
        try:
            import ray  # type: ignore[import-untyped]
            from ray import train  # type: ignore[import-untyped]
            from ray.train.torch import TorchTrainer  # type: ignore[import-untyped]

            ray_address = job.config.ray_address or "auto"
            if not ray.is_initialized():
                ray.init(address=ray_address, ignore_reinit_error=True)

            job.status = JobStatus.TRAINING
            logger.info(
                f"Ray Train 训练已启动: job_id={job.job_id}, "
                f"address={ray_address}"
            )

            # Ray Train stub — 真实训练循环待实现
            # 实际使用时应构建 TorchTrainer + ScalingConfig
            self._run_training_stub(job)

        except ImportError:
            logger.warning(
                "Ray 未安装，回退到本地训练桩。"
                "安装: pip install 'ray[train]'"
            )
            self._start_local_training(job)

    def _start_local_training(self, job: FineTuneJob) -> None:
        """使用本地训练桩。"""
        job.status = JobStatus.TRAINING
        logger.info(f"本地训练桩已启动: job_id={job.job_id}")
        self._run_training_stub(job)

    def _run_training_stub(self, job: FineTuneJob) -> None:
        """训练桩 — 记录意图，模拟指标，不执行真实训练。

        等待积累 50K+ 条轨迹后接入真实训练循环。
        """
        cfg = job.config
        logger.info(
            f"[训练桩] job_id={job.job_id}: "
            f"model={cfg.model_name}, "
            f"dataset={cfg.dataset_path}, "
            f"epochs={cfg.epochs}, "
            f"lr={cfg.learning_rate}, "
            f"batch_size={cfg.batch_size}, "
            f"use_lora={cfg.use_lora} (rank={cfg.lora_rank})"
        )

        # 模拟训练指标（用于测试管线连通性）
        for epoch in range(1, cfg.epochs + 1):
            # 每 epoch 模拟 3 个 checkpoint
            steps_per_epoch = 3
            for step_in_epoch in range(1, steps_per_epoch + 1):
                global_step = (epoch - 1) * steps_per_epoch + step_in_epoch
                simulated_loss = max(0.1, 2.0 - global_step * 0.15)
                simulated_accuracy = min(0.99, 0.3 + global_step * 0.08)

                metrics = TrainingMetrics(
                    step=global_step,
                    epoch=epoch,
                    loss=round(simulated_loss, 4),
                    eval_loss=round(simulated_loss * 1.1, 4),
                    accuracy=round(simulated_accuracy, 4),
                    learning_rate=cfg.learning_rate,
                )
                job.metrics.append(metrics)

                logger.info(
                    f"[训练桩] job_id={job.job_id} "
                    f"epoch={epoch}/{cfg.epochs} "
                    f"step={global_step} "
                    f"loss={metrics.loss:.4f} "
                    f"accuracy={metrics.accuracy:.4f}"
                )

        # 训练桩完成
        job.status = JobStatus.COMPLETED
        job.completed_at = datetime.now()
        job.output_path = str(
            Path(cfg.output_dir) / f"stub_model_{job.job_id[:8]}"
        )

        # 记录最佳指标
        if job.metrics:
            job.best_metrics = min(job.metrics, key=lambda m: m.loss)

        logger.info(
            f"[训练桩] job_id={job.job_id} 完成, "
            f"耗时 {job.duration_seconds:.1f}s, "
            f"output={job.output_path}"
        )
