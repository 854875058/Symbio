"""Core configuration management with YAML + environment variable support."""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Optional

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
    port: int = Field(default=8000, description="Server port")
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

    model_config = {"env_prefix": "SYMBIO_HITL_"}


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

    model_config = {"env_prefix": "SYMBIO_"}

    @classmethod
    def from_yaml(cls, path: str | Path) -> Settings:
        """Load settings from YAML file."""
        path = Path(path)
        if not path.exists():
            return cls()

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        return cls(**data)

    def to_yaml(self, path: str | Path) -> None:
        """Save settings to YAML file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(self.model_dump(), f, default_flow_style=False, allow_unicode=True)


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    settings = Settings()
    if settings.config_file:
        settings = Settings.from_yaml(settings.config_file)
    return settings
