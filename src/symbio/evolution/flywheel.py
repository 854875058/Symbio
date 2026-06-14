"""数据飞轮门面：把四个阶段串成统一、可被 API/UI 调用的闭环。

兑现公众号《数据飞轮：让 Agent 越用越聪明》承诺的四阶段：
1. 轨迹捕获  —— AsyncTrajectoryCapture（已存在，这里负责把统计暴露出来）
2. 失效分析  —— PatternAnalyzer：记录失败、归纳根因、识别成功路径
3. SOP 蒸馏  —— SOPDistiller + SeedSOP：从成功轨迹蒸馏标准操作流程
4. 反哺优化  —— FeedbackCollector：显式/隐式反馈统计，驱动后续优化

PatternAnalyzer / FeedbackCollector 都是 SQLite-backed，需要 connect()/close()。
本门面以进程级单例懒连接，所有读取失败都降级为空结果而非抛错。
蒸馏出的 SOP 持久化到 data/distilled_sops.json，与内置种子 SOP 合并展示。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from symbio.utils.logger import get_logger

logger = get_logger("flywheel")

_DATA_DIR = Path("data")
_ANALYSIS_DB = str(_DATA_DIR / "analysis.db")
_FEEDBACK_DB = str(_DATA_DIR / "feedback.db")
_DISTILLED_SOP_PATH = _DATA_DIR / "distilled_sops.json"


class DataFlywheel:
    """数据飞轮门面（进程级单例，通过 get_flywheel 获取）。"""

    def __init__(
        self,
        analysis_db: str = _ANALYSIS_DB,
        feedback_db: str = _FEEDBACK_DB,
        distilled_path: Path = _DISTILLED_SOP_PATH,
    ) -> None:
        self._analysis_db = analysis_db
        self._feedback_db = feedback_db
        self._distilled_path = Path(distilled_path)
        self._analyzer: Any = None
        self._feedback: Any = None
        self._distiller: Any = None

    # ------------------------------------------------------------------
    # 懒加载组件
    # ------------------------------------------------------------------

    async def _get_analyzer(self) -> Any:
        if self._analyzer is None:
            from symbio.evolution.analyzer import PatternAnalyzer

            Path(self._analysis_db).parent.mkdir(parents=True, exist_ok=True)
            analyzer = PatternAnalyzer(db_path=self._analysis_db)
            await analyzer.connect()
            self._analyzer = analyzer
        return self._analyzer

    async def _get_feedback(self) -> Any:
        if self._feedback is None:
            from symbio.evolution.feedback import FeedbackCollector

            Path(self._feedback_db).parent.mkdir(parents=True, exist_ok=True)
            collector = FeedbackCollector(db_path=self._feedback_db)
            await collector.connect()
            self._feedback = collector
        return self._feedback

    def _get_distiller(self) -> Any:
        if self._distiller is None:
            from symbio.evolution.sop_distiller import SOPDistiller

            self._distiller = SOPDistiller()
        return self._distiller

    async def close(self) -> None:
        if self._analyzer is not None:
            await self._analyzer.close()
            self._analyzer = None
        if self._feedback is not None:
            await self._feedback.close()
            self._feedback = None

    # ------------------------------------------------------------------
    # 阶段一：轨迹捕获统计（来自全局 AsyncTrajectoryCapture 若已挂载）
    # ------------------------------------------------------------------

    def trajectory_stats(self, capture: Any = None) -> dict[str, Any]:
        if capture is None:
            return {"available": False, "captured": 0, "queued": 0, "written": 0}
        try:
            stats = capture.get_stats()
            data = stats.model_dump(mode="json") if hasattr(stats, "model_dump") else dict(stats)
            data["available"] = True
            return data
        except Exception as e:
            logger.warning(f"读取轨迹捕获统计失败: {e}")
            return {"available": False}

    # ------------------------------------------------------------------
    # 阶段二：失效分析
    # ------------------------------------------------------------------

    async def analysis_summary(self) -> dict[str, Any]:
        try:
            analyzer = await self._get_analyzer()
            result = await analyzer.get_analysis_summary()
            data = result.model_dump(mode="json")
            data["available"] = True
            return data
        except Exception as e:
            logger.warning(f"读取失效分析摘要失败: {e}")
            return {"available": False, "error": str(e)}

    async def list_failures(self, limit: int = 50) -> list[dict[str, Any]]:
        try:
            analyzer = await self._get_analyzer()
            return await analyzer.get_failure_analyses(limit=limit)
        except Exception as e:
            logger.warning(f"读取失败分析列表失败: {e}")
            return []

    async def list_root_causes(self, limit: int = 50) -> list[dict[str, Any]]:
        try:
            analyzer = await self._get_analyzer()
            return await analyzer.get_root_causes(limit=limit)
        except Exception as e:
            logger.warning(f"读取根因列表失败: {e}")
            return []

    async def record_failure(self, payload: dict[str, Any]) -> dict[str, Any]:
        """记录一次失败分析（驱动闭环演示）。"""
        from symbio.evolution.analyzer import FailureAnalysis, FailureCategory, FailureSeverity

        analyzer = await self._get_analyzer()
        try:
            category = FailureCategory(payload.get("category", "unknown"))
        except ValueError:
            category = FailureCategory.UNKNOWN
        try:
            severity = FailureSeverity(payload.get("severity", "medium"))
        except ValueError:
            severity = FailureSeverity.MEDIUM

        analysis = FailureAnalysis(
            task_id=payload.get("task_id", ""),
            trajectory_id=payload.get("trajectory_id", ""),
            prompt_id=payload.get("prompt_id", ""),
            category=category,
            severity=severity,
            description=payload.get("description", ""),
            error_message=payload.get("error_message", ""),
            steps_to_failure=int(payload.get("steps_to_failure", 0) or 0),
        )
        analysis_id = await analyzer.analyze_failure(analysis)
        return {"analysis_id": analysis_id}

    # ------------------------------------------------------------------
    # 阶段三：SOP 蒸馏
    # ------------------------------------------------------------------

    def _load_distilled(self) -> list[dict[str, Any]]:
        if not self._distilled_path.exists():
            return []
        try:
            return json.loads(self._distilled_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"读取已蒸馏 SOP 失败: {e}")
            return []

    def _save_distilled(self, sops: list[dict[str, Any]]) -> None:
        self._distilled_path.parent.mkdir(parents=True, exist_ok=True)
        self._distilled_path.write_text(
            json.dumps(sops, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def list_sops(self) -> dict[str, Any]:
        from symbio.evolution.sop_distiller import SeedSOP

        seeds = []
        try:
            for sop in SeedSOP.get_seeds():
                d = sop.model_dump(mode="json")
                d["source"] = "seed"
                seeds.append(d)
        except Exception as e:
            logger.warning(f"读取种子 SOP 失败: {e}")

        distilled = self._load_distilled()
        for d in distilled:
            d.setdefault("source", "distilled")

        return {
            "seeds": seeds,
            "distilled": distilled,
            "seed_count": len(seeds),
            "distilled_count": len(distilled),
            "total": len(seeds) + len(distilled),
        }

    def distill_from_trajectory(self, payload: dict[str, Any]) -> dict[str, Any]:
        """从一条成功轨迹蒸馏 SOP 并持久化。"""
        from symbio.evolution.sop_distiller import TrajectoryData
        from symbio.utils.types import TrajectoryStep

        distiller = self._get_distiller()
        steps = []
        for i, s in enumerate(payload.get("steps", [])):
            if isinstance(s, dict):
                steps.append(TrajectoryStep(
                    step_id=str(s.get("step_id", i)),
                    thought=s.get("thought", ""),
                    action=s.get("action", ""),
                    observation=s.get("observation", s.get("result", "")),
                ))
        trajectory = TrajectoryData(
            trajectory_id=payload.get("trajectory_id", ""),
            task_type=payload.get("task_type", "general"),
            steps=steps,
            success=bool(payload.get("success", True)),
            token_count=int(payload.get("token_count", 0) or 0),
            duration_ms=int(payload.get("duration_ms", 0) or 0),
        )
        sop = distiller.distill(trajectory)
        if sop is None:
            return {"distilled": False, "reason": "轨迹未达到 SOP 蒸馏质量门槛"}

        record = sop.model_dump(mode="json")
        record["source"] = "distilled"
        existing = self._load_distilled()
        existing.append(record)
        self._save_distilled(existing)
        return {"distilled": True, "sop": record}

    # ------------------------------------------------------------------
    # 阶段四：反哺优化（反馈）
    # ------------------------------------------------------------------

    async def feedback_stats(self) -> dict[str, Any]:
        try:
            collector = await self._get_feedback()
            stats = await collector.get_stats()
            data = stats.model_dump(mode="json")
            data["available"] = True
            return data
        except Exception as e:
            logger.warning(f"读取反馈统计失败: {e}")
            return {"available": False, "error": str(e)}

    async def collect_feedback(self, payload: dict[str, Any]) -> dict[str, Any]:
        from symbio.evolution.feedback import ExplicitFeedback

        collector = await self._get_feedback()
        feedback = ExplicitFeedback(
            session_id=payload.get("session_id", ""),
            task_id=payload.get("task_id", ""),
            prompt_id=payload.get("prompt_id", ""),
            user_id=payload.get("user_id", ""),
            rating=float(payload.get("rating", 0) or 0),
            comment=payload.get("comment", ""),
            tags=payload.get("tags", []) or [],
        )
        feedback_id = await collector.collect_explicit(feedback)
        return {"feedback_id": feedback_id}

    # ------------------------------------------------------------------
    # 总览
    # ------------------------------------------------------------------

    async def overview(self) -> dict[str, Any]:
        summary = await self.analysis_summary()
        feedback = await self.feedback_stats()
        sops = self.list_sops()
        return {
            "stages": {
                "capture": {"name": "轨迹捕获", "status": "active"},
                "analysis": {
                    "name": "失效分析",
                    "total_failures": summary.get("total_failures", 0),
                    "total_root_causes": summary.get("total_root_causes", 0),
                    "failure_rate": summary.get("failure_rate", 0.0),
                },
                "distillation": {
                    "name": "SOP 蒸馏",
                    "seed_count": sops["seed_count"],
                    "distilled_count": sops["distilled_count"],
                },
                "feedback": {
                    "name": "反哺优化",
                    "total_explicit": feedback.get("total_explicit", 0),
                    "total_implicit": feedback.get("total_implicit", 0),
                    "average_rating": feedback.get("average_rating", 0.0),
                    "acceptance_rate": feedback.get("acceptance_rate", 0.0),
                },
            },
            "analysis_summary": summary,
            "feedback_stats": feedback,
            "sop_counts": {
                "seed": sops["seed_count"],
                "distilled": sops["distilled_count"],
                "total": sops["total"],
            },
        }


_flywheel: Optional[DataFlywheel] = None


def get_flywheel() -> DataFlywheel:
    global _flywheel
    if _flywheel is None:
        _flywheel = DataFlywheel()
    return _flywheel


def reset_flywheel() -> None:
    global _flywheel
    _flywheel = None
