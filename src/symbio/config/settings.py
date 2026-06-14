"""Core configuration management with YAML + environment variable support."""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class ModelConfig(BaseSettings):
    """LLM model configuration."""

    # Anthropic
    anthropic_api_key: str = Field(default="", description="Anthropic API key")
    anthropic_base_url: str = Field(default="https://api.anthropic.com", description="Anthropic base URL")

    # OpenAI compatible (for local models)
    openai_api_key: str = Field(default="", description="OpenAI API key")
    openai_base_url: str = Field(default="https://api.openai.com/v1", description="OpenAI base URL")

    # Model routing
    model_low: str = Field(default="claude-3-5-haiku-20241022", description="Model for simple tasks")
    model_medium: str = Field(default="claude-sonnet-4-20250514", description="Model for medium tasks")
    model_high: str = Field(default="claude-opus-4-20250514", description="Model for complex tasks")

    # Local model fallback
    local_model_enabled: bool = Field(default=False, description="Enable local model fallback")
    local_model_url: str = Field(default="http://localhost:11434", description="Local model URL (Ollama)")
    local_model_name: str = Field(default="qwen2.5:14b", description="Local model name")

    model_config = {"env_prefix": "SYMBIO_MODEL_"}


class MemoryConfig(BaseSettings):
    """Memory system configuration."""

    # LanceDB
    lancedb_path: str = Field(default="./data/lancedb", description="LanceDB storage path")
    embedding_model: str = Field(default="text-embedding-3-small", description="Embedding model")
    embedding_dim: int = Field(default=1536, description="Embedding dimension")

    # Short-term memory
    window_size: int = Field(default=20, description="Conversation window size")
    auto_summary_threshold: int = Field(default=50, description="Auto-summary threshold (messages)")

    # Long-term memory
    max_memories: int = Field(default=10000, description="Max stored memories")
    similarity_threshold: float = Field(default=0.7, description="Memory recall similarity threshold")

    model_config = {"env_prefix": "SYMBIO_MEMORY_"}


class ToolConfig(BaseSettings):
    """Tool execution configuration."""

    # Shell
    shell_timeout: int = Field(default=60, description="Shell command timeout (seconds)")
    shell_blocked_commands: list[str] = Field(
        default=["rm -rf /", "mkfs", "dd if=", ":(){ :|:& };:"],
        description="Blocked shell commands"
    )

    # File
    file_max_size_mb: int = Field(default=10, description="Max file size for read/write (MB)")

    # Git
    git_auto_commit: bool = Field(default=False, description="Auto-commit after changes")

    model_config = {"env_prefix": "SYMBIO_TOOL_"}


class ServerConfig(BaseSettings):
    """Server configuration."""

    host: str = Field(default="0.0.0.0", description="Server host")
    port: int = Field(default=9090, description="Server port")
    debug: bool = Field(default=False, description="Debug mode")

    # WebSocket
    ws_heartbeat_interval: int = Field(default=30, description="WebSocket heartbeat interval")

    model_config = {"env_prefix": "SYMBIO_SERVER_"}


class EvolutionConfig(BaseSettings):
    """Evolution engine configuration."""

    feedback_enabled: bool = Field(default=True, description="Enable feedback collection")
    auto_review: bool = Field(default=True, description="Auto-review failed tasks")
    sop_extraction: bool = Field(default=True, description="Extract SOP from successful paths")

    # Data flywheel
    trajectory_capture: bool = Field(default=True, description="Capture execution trajectories")
    dataset_export_format: str = Field(default="sharegpt", description="Dataset export format")

    model_config = {"env_prefix": "SYMBIO_EVOLUTION_"}


class HITLConfig(BaseSettings):
    """Human-in-the-loop configuration."""

    enabled: bool = Field(default=True, description="Enable HITL")
    high_risk_auto_suspend: bool = Field(default=True, description="Auto-suspend high-risk actions")
    approval_timeout: int = Field(default=300, description="Approval timeout (seconds)")

    # IM notification
    notify_platform: str = Field(default="", description="Notification platform (qq/wechat/feishu)")
    notify_chat_id: str = Field(default="", description="Notification chat ID")
    notify_endpoint: str = Field(default="", description="Notification bridge endpoint")
    notify_chat_type: str = Field(default="group", description="Chat type (group/private)")
    notify_access_token: str = Field(default="", description="Notification bridge access token")
    notify_secret: str = Field(default="", description="Notification platform signing secret")
    notify_targets: list[dict[str, Any]] = Field(default_factory=list, description="Notification targets")
    notify_timeout: float = Field(default=5.0, description="Notification HTTP timeout (seconds)")
    callback_base_url: str = Field(default="", description="Public API base URL for approval links")
    im_webhook_token: str = Field(default="", description="Shared token for IM approval callbacks")

    model_config = {"env_prefix": "SYMBIO_HITL_"}


class CostConfig(BaseSettings):
    """Token cost optimization configuration (semantic cache / context pruning / budget)."""

    semantic_cache_enabled: bool = Field(default=True, description="Enable semantic cache on chat path (requires OpenAI embedding key)")
    context_max_tokens: int = Field(default=8000, description="Context pruning target budget (tokens)")
    budget_project_id: str = Field(default="default", description="Default budget project id")
    monthly_budget_tokens: int = Field(default=0, description="Monthly token budget, 0 = unlimited")

    model_config = {"env_prefix": "SYMBIO_COST_"}


class OtelConfig(BaseSettings):
    """OpenTelemetry tracing configuration."""

    enabled: bool = Field(default=False, description="Enable OpenTelemetry tracing")
    exporter: str = Field(default="otlp", description="Exporter type (jaeger/otlp/console)")
    endpoint: str = Field(default="http://localhost:4317", description="OTLP gRPC endpoint (works with Jaeger >= 1.35)")
    service_name: str = Field(default="symbio", description="OTel service name")

    model_config = {"env_prefix": "SYMBIO_OTEL_"}


class Settings(BaseSettings):
    """Root configuration."""

    # General
    app_name: str = Field(default="Symbio", description="Application name")
    version: str = Field(default="0.1.0", description="Application version")
    log_level: LogLevel = Field(default=LogLevel.INFO, description="Log level")
    log_file: Optional[str] = Field(default=None, description="Log file path")
    config_file: Optional[str] = Field(default=None, description="Config file path")

    # Sub-configs
    model: ModelConfig = Field(default_factory=ModelConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    tool: ToolConfig = Field(default_factory=ToolConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    evolution: EvolutionConfig = Field(default_factory=EvolutionConfig)
    hitl: HITLConfig = Field(default_factory=HITLConfig)
    cost: CostConfig = Field(default_factory=CostConfig)
    otel: OtelConfig = Field(default_factory=OtelConfig)

    model_config = {"env_prefix": "SYMBIO_"}

    @classmethod
    def from_yaml(cls, path: str | Path) -> Settings:
        """Load settings from YAML file."""
        path = Path(path)
        if not path.exists():
            return cls()

        class _SettingsSafeLoader(yaml.SafeLoader):
            pass

        def _construct_legacy_log_level(loader: yaml.SafeLoader, node: yaml.Node) -> str:
            value = loader.construct_sequence(node)
            return str(value[0]) if value else LogLevel.INFO.value

        _SettingsSafeLoader.add_constructor(
            "tag:yaml.org,2002:python/object/apply:symbio.config.settings.LogLevel",
            _construct_legacy_log_level,
        )

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.load(f, Loader=_SettingsSafeLoader) or {}

        return cls(**data)

    def to_yaml(self, path: str | Path) -> None:
        """Save settings to YAML file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                self.model_dump(mode="json"),
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    settings = Settings()
    if settings.config_file:
        settings = Settings.from_yaml(settings.config_file)
    return settings
