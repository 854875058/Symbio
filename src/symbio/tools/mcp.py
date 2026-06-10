"""Minimal MCP stdio client and Symbio tool bridge.

This implements the practical subset needed to mount standard MCP tools:
initialize, tools/list and tools/call over JSON-RPC on stdio. It intentionally
does not claim resource, prompt or sampling support yet.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from symbio.tools.registry import (
    BaseTool,
    PermissionLevel,
    ToolPermission,
    ToolRegistry,
    ToolSchema,
)
from symbio.utils.logger import get_logger
from symbio.utils.types import ToolResult

logger = get_logger("tools.mcp")


class MCPError(RuntimeError):
    """Raised when an MCP server returns a JSON-RPC error."""


@dataclass
class MCPToolSpec:
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class MCPServerConfig:
    """Configuration for an MCP stdio server.

    Authentication is intentionally limited to explicit env and metadata fields.
    """

    name: str
    command: str | list[str]
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    name_prefix: Optional[str] = None

    @property
    def command_line(self) -> str | list[str]:
        if isinstance(self.command, str):
            return [self.command, *self.args] if self.args else self.command
        return [*self.command, *self.args]


class MCPStdioClient:
    """JSON-RPC over stdio MCP client for tool discovery and invocation."""

    def __init__(
        self,
        command: str | list[str],
        *,
        name: str = "mcp",
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        metadata: Optional[dict[str, Any]] = None,
        timeout: float = 30.0,
    ) -> None:
        self.name = name
        self.command = shlex.split(command) if isinstance(command, str) else list(command)
        self.cwd = cwd
        self.env = env
        self.metadata = metadata or {}
        self.timeout = timeout
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._next_id = 1
        self._lock = asyncio.Lock()

    @property
    def is_started(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        if self.is_started:
            return
        if not self.command:
            raise ValueError("MCP command is empty")

        self._proc = await asyncio.create_subprocess_exec(
            *self.command,
            cwd=self.cwd,
            env=self.env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await self.initialize()

    async def close(self) -> None:
        proc = self._proc
        self._proc = None
        if not proc or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

    async def initialize(self) -> dict[str, Any]:
        result = await self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "symbio", "version": "0.1.0"},
            },
            auto_start=False,
        )
        await self.notify("notifications/initialized", {})
        return result

    async def list_tools(self) -> list[MCPToolSpec]:
        await self.start()
        result = await self.request("tools/list", {})
        tools = result.get("tools", []) if isinstance(result, dict) else []
        specs = []
        for item in tools:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            specs.append(
                MCPToolSpec(
                    name=str(item["name"]),
                    description=str(item.get("description", "")),
                    input_schema=item.get("inputSchema") or item.get("input_schema") or {},
                )
            )
        return specs

    async def call_tool(self, tool_name: str, arguments: Optional[dict[str, Any]] = None) -> Any:
        await self.start()
        return await self.request(
            "tools/call",
            {"name": tool_name, "arguments": arguments or {}},
        )

    async def notify(self, method: str, params: Optional[dict[str, Any]] = None) -> None:
        proc = self._require_proc()
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        assert proc.stdin is not None
        proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await proc.stdin.drain()

    async def request(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
        *,
        auto_start: bool = True,
    ) -> Any:
        if auto_start:
            await self.start()
        proc = self._require_proc()

        async with self._lock:
            request_id = self._next_id
            self._next_id += 1
            payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            }

            assert proc.stdin is not None
            proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            await proc.stdin.drain()

            while True:
                line = await asyncio.wait_for(self._readline(), timeout=self.timeout)
                if not line:
                    raise MCPError(f"MCP server {self.name} closed stdout")
                message = json.loads(line)
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    error = message["error"]
                    raise MCPError(error.get("message", str(error)) if isinstance(error, dict) else str(error))
                return message.get("result")

    async def _readline(self) -> str:
        proc = self._require_proc()
        assert proc.stdout is not None
        data = await proc.stdout.readline()
        return data.decode("utf-8", errors="replace").strip()

    def _require_proc(self) -> asyncio.subprocess.Process:
        if not self._proc or self._proc.returncode is not None:
            raise RuntimeError(f"MCP server {self.name} is not running")
        return self._proc


class MCPTool(BaseTool):
    """Symbio Tool wrapper around a remote MCP tool."""

    version = "1.0.0"
    author = "symbio"
    tags = ["mcp", "external"]
    permission = ToolPermission(level=PermissionLevel.READ_ONLY, requires_approval=False)

    def __init__(
        self,
        client: MCPStdioClient,
        spec: MCPToolSpec,
        *,
        name_prefix: str = "mcp",
        permission: Optional[ToolPermission] = None,
    ) -> None:
        safe_name = spec.name.replace("/", "_").replace(" ", "_")
        self.name = f"{name_prefix}_{safe_name}" if name_prefix else safe_name
        self.description = spec.description or f"MCP tool {spec.name}"
        self.client = client
        self.remote_name = spec.name
        self.input_schema = spec.input_schema or {"type": "object", "properties": {}}
        if permission is not None:
            self.permission = permission

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters=self.input_schema,
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            result = await self.client.call_tool(self.remote_name, kwargs)
            return ToolResult(
                call_id="",
                tool_name=self.name,
                success=True,
                output=_stringify_mcp_result(result),
            )
        except Exception as exc:
            return ToolResult(
                call_id="",
                tool_name=self.name,
                success=False,
                error=str(exc),
            )


async def register_mcp_stdio_tools(
    registry: ToolRegistry,
    command: str | list[str],
    *,
    name: str = "mcp",
    cwd: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    metadata: Optional[dict[str, Any]] = None,
    name_prefix: Optional[str] = None,
    permission: Optional[ToolPermission] = None,
) -> list[MCPTool]:
    """Discover tools from an MCP stdio server and register them."""
    client = MCPStdioClient(command, name=name, cwd=cwd, env=env, metadata=metadata)
    specs = await client.list_tools()
    tools = [
        MCPTool(
            client,
            spec,
            name_prefix=name_prefix if name_prefix is not None else name,
            permission=permission,
        )
        for spec in specs
    ]
    registry.register_many(tools)
    logger.info("Registered %d MCP tool(s) from %s", len(tools), name)
    return tools


def load_mcp_config(source: dict[str, Any] | list[Any] | str | Path) -> list[MCPServerConfig]:
    """Load MCP server configs from a dict/list, JSON file, or YAML file."""
    if isinstance(source, (str, Path)):
        path = Path(source)
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"Unable to read MCP config file {path}: {exc}") from exc

        suffix = path.suffix.lower()
        try:
            if suffix == ".json":
                data = json.loads(content)
            elif suffix in {".yaml", ".yml"}:
                data = yaml.safe_load(content) or {}
            else:
                raise ValueError(f"Unsupported MCP config file type: {path.suffix}")
        except (json.JSONDecodeError, yaml.YAMLError) as exc:
            raise ValueError(f"Invalid MCP config file {path}: {exc}") from exc
        return discover_mcp_servers(data)

    return discover_mcp_servers(source)


def discover_mcp_servers(data: dict[str, Any] | list[Any]) -> list[MCPServerConfig]:
    """Parse common MCP server config schemas into normalized server configs."""
    if isinstance(data, dict):
        raw_servers = data.get("mcpServers", data.get("servers", data))
        if isinstance(raw_servers, dict):
            return [
                _parse_mcp_server_config(name, value)
                for name, value in raw_servers.items()
            ]
        if isinstance(raw_servers, list):
            return [_parse_mcp_server_config(None, value) for value in raw_servers]
        raise ValueError("MCP config must contain a mapping or list of servers")

    if isinstance(data, list):
        return [_parse_mcp_server_config(None, value) for value in data]

    raise ValueError("MCP config must be a dict, list, JSON file, or YAML file")


async def register_mcp_configured_tools(
    registry: ToolRegistry,
    config: dict[str, Any] | list[Any] | str | Path,
    *,
    permission: Optional[ToolPermission] = None,
) -> list[MCPTool]:
    """Discover configured MCP servers and register their tools."""
    tools: list[MCPTool] = []
    for server in load_mcp_config(config):
        registered = await register_mcp_stdio_tools(
            registry,
            server.command_line,
            name=server.name,
            cwd=server.cwd,
            env=server.env or None,
            metadata=server.metadata,
            name_prefix=server.name_prefix,
            permission=permission,
        )
        tools.extend(registered)
    return tools


def _parse_mcp_server_config(
    name: Optional[str],
    value: Any,
) -> MCPServerConfig:
    if not isinstance(value, dict):
        raise ValueError("MCP server config must be a mapping")

    server_name = str(value.get("name") or name or "")
    if not server_name:
        raise ValueError("MCP server config requires a name")

    command = value.get("command")
    args = value.get("args", [])

    if not command:
        raise ValueError(f"MCP server {server_name} requires command")
    if not isinstance(command, (str, list)):
        raise ValueError(f"MCP server {server_name} command must be a string or list")
    if isinstance(command, list) and not all(isinstance(item, str) for item in command):
        raise ValueError(f"MCP server {server_name} command list must contain strings")
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise ValueError(f"MCP server {server_name} args must be a list of strings")

    env = value.get("env", {})
    if env is None:
        env = {}
    if not isinstance(env, dict) or not all(
        isinstance(key, str) and isinstance(val, str) for key, val in env.items()
    ):
        raise ValueError(f"MCP server {server_name} env must be a string mapping")

    metadata = value.get("metadata", {})
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise ValueError(f"MCP server {server_name} metadata must be a mapping")

    cwd = value.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise ValueError(f"MCP server {server_name} cwd must be a string")

    name_prefix = value.get("name_prefix", value.get("namePrefix"))
    if name_prefix is not None and not isinstance(name_prefix, str):
        raise ValueError(f"MCP server {server_name} name_prefix must be a string")

    return MCPServerConfig(
        name=server_name,
        command=command,
        args=args,
        env=dict(env),
        cwd=cwd,
        metadata=dict(metadata),
        name_prefix=name_prefix,
    )


def _stringify_mcp_result(result: Any) -> str:
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            parts = []
            for item in content:
                if not isinstance(item, dict):
                    parts.append(str(item))
                elif item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            return "\n".join(p for p in parts if p)
        if "structuredContent" in result:
            return json.dumps(result["structuredContent"], ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False) if not isinstance(result, str) else result


def mcp_server_script() -> str:
    """Small stdio MCP server used by tests and smoke checks."""
    return r'''
import json
import sys

TOOLS = [{
    "name": "echo",
    "description": "Echo text",
    "inputSchema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
}]

for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    method = msg.get("method")
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "test"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        args = msg.get("params", {}).get("arguments", {})
        result = {"content": [{"type": "text", "text": args.get("text", "")}]}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "not found"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
'''


def test_server_command() -> list[str]:
    return [sys.executable, "-u", "-c", mcp_server_script()]
