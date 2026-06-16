"""MCP 工具网关完善测试：context manager、resources/prompts、连接池、挂载。"""

from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.tools.mcp import (
    MCPConnectionPool,
    MCPStdioClient,
    get_mcp_pool,
    reset_mcp_pool,
    test_server_command as mcp_test_server_command,
)
from symbio.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# 异步上下文管理器（此前缺失，导致 probe 端点其实跑不通）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_client_async_context_manager():
    async with MCPStdioClient(mcp_test_server_command(), name="t") as client:
        assert client.is_started
        tools = await client.list_tools()
        assert any(t.name == "echo" for t in tools)
    # 退出后进程关闭
    assert not client.is_started


@pytest.mark.asyncio
async def test_initialize_captures_capabilities():
    async with MCPStdioClient(mcp_test_server_command(), name="t") as client:
        assert client.supports("tools")
        assert client.supports("resources")
        assert client.supports("prompts")
        assert client.server_info.get("name") == "test"


# ---------------------------------------------------------------------------
# resources / prompts 协议
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_resources_and_read():
    async with MCPStdioClient(mcp_test_server_command(), name="t") as client:
        resources = await client.list_resources()
        assert len(resources) == 1
        assert resources[0]["uri"] == "mem://note"
        body = await client.read_resource("mem://note")
        assert "resource body" in str(body)


@pytest.mark.asyncio
async def test_list_prompts_and_get():
    async with MCPStdioClient(mcp_test_server_command(), name="t") as client:
        prompts = await client.list_prompts()
        assert len(prompts) == 1
        assert prompts[0]["name"] == "greet"
        got = await client.get_prompt("greet")
        assert "messages" in got


# ---------------------------------------------------------------------------
# 连接池复用
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connection_pool_reuses_client():
    pool = MCPConnectionPool()
    try:
        c1 = await pool.get_client("srv", mcp_test_server_command())
        c2 = await pool.get_client("srv", mcp_test_server_command())
        assert c1 is c2  # 复用同一连接，不重启进程
        assert "srv" in pool.active_servers()
    finally:
        await pool.close_all()
        assert pool.active_servers() == []


@pytest.mark.asyncio
async def test_pool_rebuilds_dead_client():
    pool = MCPConnectionPool()
    try:
        c1 = await pool.get_client("srv", mcp_test_server_command())
        await c1.close()  # 模拟进程死亡
        c2 = await pool.get_client("srv", mcp_test_server_command())
        assert c2.is_started
        assert c2 is not c1
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_pool_singleton_reset():
    reset_mcp_pool_sync = reset_mcp_pool
    p1 = get_mcp_pool()
    p2 = get_mcp_pool()
    assert p1 is p2
    await reset_mcp_pool_sync()
    p3 = get_mcp_pool()
    assert p3 is not p1


# ---------------------------------------------------------------------------
# 挂载进全局注册中心
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mount_tools_into_registry():
    from symbio.tools.mcp import MCPTool
    registry = ToolRegistry()
    async with MCPStdioClient(mcp_test_server_command(), name="srv") as client:
        specs = await client.list_tools()
        for spec in specs:
            registry.register(MCPTool(client, spec, name_prefix="srv"))
        # 工具以 srv_<name> 命名进入注册中心
        names = registry.list_tools()
        assert "srv_echo" in names
        # 调用挂载的工具确实能 round-trip 到 MCP server
        tool = registry.get("srv_echo")
        result = await tool.execute(text="hello-mcp")
        assert result.success
        assert "hello-mcp" in result.output
