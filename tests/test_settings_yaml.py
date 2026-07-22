"""Settings YAML compatibility tests."""

import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.config.settings import LogLevel, Settings


def test_from_yaml_loads_legacy_log_level_python_tag(tmp_path):
    config_path = tmp_path / "symbio.yaml"
    config_path.write_text(
        "\n".join(
            [
                "app_name: Symbio",
                "log_level: !!python/object/apply:symbio.config.settings.LogLevel",
                "- INFO",
                "model:",
                "  anthropic_api_key: test-key",
            ]
        ),
        encoding="utf-8",
    )

    settings = Settings.from_yaml(config_path)

    assert settings.log_level == LogLevel.INFO
    assert settings.model.anthropic_api_key == "test-key"


def test_to_yaml_writes_safe_yaml_without_python_tags(tmp_path):
    config_path = tmp_path / "symbio.yaml"
    Settings(log_level=LogLevel.DEBUG).to_yaml(config_path)

    raw = config_path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)

    assert "!!python" not in raw
    assert data["log_level"] == "DEBUG"
