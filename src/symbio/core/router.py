"""模型路由器 - 前端可配置的智能路由矩阵"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from symbio.config.settings import get_settings
from symbio.utils.logger import get_logger
from symbio.utils.types import TaskComplexity

logger = get_logger("router")


class ModelInfo(BaseModel):
    """模型信息"""
    model_id: str
    provider: str = "anthropic"
    display_name: str = ""
    is_local: bool = False
    enabled: bool = True
    cost_per_1k_input: float = 0.0
    cost_per_1k_output: float = 0.0
    max_tokens: int = 4096


class TaskModelBinding(BaseModel):
    """任务-模型绑定策略"""
    task_type: str
    complexity: TaskComplexity
    preferred_model: str
    fallback_model: str = ""


class ModelRouter:
    """可配置的模型路由器

    用户在 Web UI 中自由配置模型池和任务-模型绑定策略。
    """

    def __init__(self):
        self.settings = get_settings()
        self._model_pool: dict[str, ModelInfo] = {}
        self._bindings: list[TaskModelBinding] = []
        self._init_default_models()

    def _init_default_models(self) -> None:
        """初始化默认模型池"""
        # Anthropic 模型
        self._model_pool["claude-3-5-haiku-20241022"] = ModelInfo(
            model_id="claude-3-5-haiku-20241022",
            provider="anthropic",
            display_name="Claude 3.5 Haiku",
            cost_per_1k_input=0.001,
            cost_per_1k_output=0.005,
        )
        self._model_pool["claude-sonnet-4-20250514"] = ModelInfo(
            model_id="claude-sonnet-4-20250514",
            provider="anthropic",
            display_name="Claude Sonnet 4",
            cost_per_1k_input=0.003,
            cost_per_1k_output=0.015,
        )
        self._model_pool["claude-opus-4-20250514"] = ModelInfo(
            model_id="claude-opus-4-20250514",
            provider="anthropic",
            display_name="Claude Opus 4",
            cost_per_1k_input=0.015,
            cost_per_1k_output=0.075,
        )

        # 本地模型（如果启用）
        if self.settings.model.local_model_enabled:
            self._model_pool[self.settings.model.local_model_name] = ModelInfo(
                model_id=self.settings.model.local_model_name,
                provider="ollama",
                display_name=f"Local ({self.settings.model.local_model_name})",
                is_local=True,
                cost_per_1k_input=0.0,
                cost_per_1k_output=0.0,
            )

    def select(
        self,
        complexity: TaskComplexity,
        task_type: str = "general",
        user_preference: Optional[str] = None,
    ) -> str:
        """选择模型

        Args:
            complexity: 任务复杂度
            task_type: 任务类型
            user_preference: 用户偏好模型

        Returns:
            模型 ID
        """
        # 1. 用户指定优先
        if user_preference and user_preference in self._model_pool:
            model = self._model_pool[user_preference]
            if model.enabled:
                logger.debug(f"使用用户指定模型: {user_preference}")
                return user_preference

        # 2. 查找绑定策略
        for binding in self._bindings:
            if binding.task_type == task_type and binding.complexity == complexity:
                if binding.preferred_model in self._model_pool:
                    model = self._model_pool[binding.preferred_model]
                    if model.enabled:
                        logger.debug(f"使用绑定模型: {binding.preferred_model}")
                        return binding.preferred_model

        # 3. 按复杂度自动选择
        model_id = self._auto_select(complexity)
        logger.debug(f"自动选择模型: {model_id}")
        return model_id

    def _auto_select(self, complexity: TaskComplexity) -> str:
        """按复杂度自动选择模型"""
        if complexity == TaskComplexity.LOW:
            # 优先本地模型
            for model in self._model_pool.values():
                if model.is_local and model.enabled:
                    return model.model_id
            return self.settings.model.model_low

        elif complexity == TaskComplexity.MEDIUM:
            return self.settings.model.model_medium

        else:  # HIGH
            return self.settings.model.model_high

    def is_available(self, model_id: str) -> bool:
        """检查模型是否在运行时池中可用且启用。"""
        model = self._model_pool.get(model_id)
        return model is not None and model.enabled

    def register_model(self, model_info: ModelInfo) -> None:
        """运行时注册新模型（供 /api/models 等热路径调用）。"""
        self._model_pool[model_info.model_id] = model_info
        logger.info(f"注册模型: {model_info.model_id} (provider={model_info.provider})")

    def unregister_model(self, model_id: str) -> bool:
        """运行时移除模型。"""
        if model_id in self._model_pool:
            del self._model_pool[model_id]
            logger.info(f"注销模型: {model_id}")
            return True
        return False

    def get_model_info(self, model_id: str) -> Optional[ModelInfo]:
        """获取模型信息"""
        return self._model_pool.get(model_id)

    def list_models(self) -> list[ModelInfo]:
        """列出所有模型"""
        return list(self._model_pool.values())

    def add_model(self, model: ModelInfo) -> None:
        """添加模型到池中"""
        self._model_pool[model.model_id] = model
        logger.info(f"添加模型: {model.model_id}")

    def remove_model(self, model_id: str) -> bool:
        """从池中移除模型"""
        if model_id in self._model_pool:
            del self._model_pool[model_id]
            logger.info(f"移除模型: {model_id}")
            return True
        return False

    def update_binding(self, binding: TaskModelBinding) -> None:
        """更新任务-模型绑定"""
        # 移除旧绑定
        self._bindings = [
            b for b in self._bindings
            if not (b.task_type == binding.task_type and b.complexity == binding.complexity)
        ]
        # 添加新绑定
        self._bindings.append(binding)
        logger.info(f"更新绑定: {binding.task_type} ({binding.complexity}) -> {binding.preferred_model}")

    def estimate_cost(
        self,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
    ) -> float:
        """估算 Token 成本（美元）"""
        model = self._model_pool.get(model_id)
        if not model:
            return 0.0

        input_cost = (input_tokens / 1000) * model.cost_per_1k_input
        output_cost = (output_tokens / 1000) * model.cost_per_1k_output
        return input_cost + output_cost
