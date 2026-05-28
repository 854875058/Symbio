"""Logging system with console + file support and level filtering."""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from symbio.config.settings import LogLevel, get_settings


def setup_logger(
    level: LogLevel | str = LogLevel.INFO,
    log_file: str | Path | None = None,
    rotation: str = "10 MB",
    retention: str = "7 days",
) -> None:
    """Configure the global logger.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR)
        log_file: Path to log file. None for console-only.
        rotation: Log rotation size/time
        retention: Log retention period
    """
    # Remove default handler
    logger.remove()

    # Console handler with color
    logger.add(
        sys.stderr,
        level=level.value if isinstance(level, LogLevel) else level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
               "<level>{level: <8}</level> | "
               "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
               "<level>{message}</level>",
        colorize=True,
    )

    # File handler
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        logger.add(
            str(log_path),
            level=level.value if isinstance(level, LogLevel) else level,
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
            rotation=rotation,
            retention=retention,
            encoding="utf-8",
        )

    logger.info(f"Logger initialized: level={level}, file={log_file}")


def get_logger(name: str = "symbio"):
    """Get a named logger instance.

    Args:
        name: Logger name (typically module name)

    Returns:
        Logger instance with bound name
    """
    return logger.bind(name=name)


def init_logger_from_settings() -> None:
    """Initialize logger from application settings."""
    settings = get_settings()
    setup_logger(
        level=settings.log_level,
        log_file=settings.log_file,
    )
