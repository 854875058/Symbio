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


def _write_config(tmp_path):
    config_path = tmp_path / "symbio.yaml"
    config_path.write_text(
        "\n".join(
            [
                "app_name: FromYaml",
                "memory:",
                "  lancedb_path: ./data/lancedb",
                "  embedding_model: text-embedding-3-small",
                "  window_size: 20",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


def test_env_var_overrides_yaml_nested_field(tmp_path, monkeypatch):
    """环境变量必须能覆盖 YAML：否则 symbio.yaml 存在时定向覆盖全部失效。"""
    config_path = _write_config(tmp_path)
    monkeypatch.setenv("SYMBIO_MEMORY_LANCEDB_PATH", "/tmp/env-wins")

    settings = Settings.from_yaml(config_path)

    assert settings.memory.lancedb_path == "/tmp/env-wins"
    # 未被环境变量指定的字段仍来自 YAML
    assert settings.memory.embedding_model == "text-embedding-3-small"
    assert settings.memory.window_size == 20
    assert settings.app_name == "FromYaml"


def test_env_var_overrides_yaml_top_level_field(tmp_path, monkeypatch):
    config_path = _write_config(tmp_path)
    monkeypatch.delenv("SYMBIO_MEMORY_LANCEDB_PATH", raising=False)
    monkeypatch.setenv("SYMBIO_APP_NAME", "FromEnv")

    settings = Settings.from_yaml(config_path)

    assert settings.app_name == "FromEnv"
    assert settings.memory.lancedb_path == "./data/lancedb"


def test_yaml_applies_when_no_env_var(tmp_path, monkeypatch):
    config_path = _write_config(tmp_path)
    monkeypatch.delenv("SYMBIO_MEMORY_LANCEDB_PATH", raising=False)
    monkeypatch.delenv("SYMBIO_APP_NAME", raising=False)

    settings = Settings.from_yaml(config_path)

    assert settings.app_name == "FromYaml"
    assert settings.memory.lancedb_path == "./data/lancedb"


def test_get_settings_respects_env_over_discovered_yaml(tmp_path, monkeypatch):
    """get_settings 自动发现 ./symbio.yaml 时也不能吞掉环境变量。"""
    from symbio.config.settings import reload_settings

    _write_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SYMBIO_CONFIG_FILE", raising=False)
    monkeypatch.setenv("SYMBIO_MEMORY_LANCEDB_PATH", "/tmp/env-wins-discovered")

    settings = reload_settings()

    assert settings.memory.lancedb_path == "/tmp/env-wins-discovered"
    assert settings.app_name == "FromYaml"
