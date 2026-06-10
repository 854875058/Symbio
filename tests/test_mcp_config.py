import json
import sys

import pytest
import yaml

from symbio.tools.mcp import (
    MCPServerConfig,
    discover_mcp_servers,
    load_mcp_config,
    register_mcp_configured_tools,
    test_server_command as mcp_test_server_command,
)
from symbio.tools.registry import ToolRegistry


def test_load_mcp_config_from_dict_mcp_servers_schema():
    config = load_mcp_config(
        {
            "mcpServers": {
                "filesystem": {
                    "command": "python",
                    "args": ["-m", "example_server"],
                    "env": {"TOKEN": "abc"},
                    "metadata": {"auth": "env"},
                }
            }
        }
    )

    assert config == [
        MCPServerConfig(
            name="filesystem",
            command="python",
            args=["-m", "example_server"],
            env={"TOKEN": "abc"},
            metadata={"auth": "env"},
        )
    ]


def test_load_mcp_config_from_json_file(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "echo": {
                        "command": sys.executable,
                        "args": ["-u", "-c", "print('ok')"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_mcp_config(path)

    assert len(config) == 1
    assert config[0].name == "echo"
    assert config[0].command == sys.executable
    assert config[0].args == ["-u", "-c", "print('ok')"]


def test_load_mcp_config_from_yaml_list(tmp_path):
    path = tmp_path / "mcp.yaml"
    path.write_text(
        yaml.safe_dump(
            [
                {
                    "name": "one",
                    "command": "python",
                    "args": ["server.py"],
                    "env": {"A": "1"},
                },
                {
                    "name": "two",
                    "command": ["python", "-m", "server"],
                },
            ]
        ),
        encoding="utf-8",
    )

    config = load_mcp_config(path)

    assert [server.name for server in config] == ["one", "two"]
    assert config[0].env == {"A": "1"}
    assert config[1].command == ["python", "-m", "server"]
    assert config[1].args == []


def test_discover_mcp_servers_rejects_invalid_config():
    with pytest.raises(ValueError, match="command"):
        discover_mcp_servers({"mcpServers": {"broken": {"args": ["server.py"]}}})

    with pytest.raises(ValueError, match="name"):
        discover_mcp_servers([{"command": "python"}])


async def test_register_mcp_configured_tools_with_test_server():
    registry = ToolRegistry()
    command = mcp_test_server_command()

    tools = await register_mcp_configured_tools(
        registry,
        {
            "mcpServers": {
                "configured": {
                    "command": command[0],
                    "args": command[1:],
                    "metadata": {"auth": "none", "source": "test"},
                }
            }
        },
    )

    try:
        assert len(tools) == 1
        assert tools[0].client.metadata == {"auth": "none", "source": "test"}
        result = await registry.execute("configured_echo", {"text": "from-config"})
        assert result.success is True
        assert result.output == "from-config"
    finally:
        await tools[0].client.close()
