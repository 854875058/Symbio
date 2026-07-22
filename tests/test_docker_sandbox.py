"""批次D9：真 Docker 沙箱执行。

覆盖：
- build_docker_run_command 纯函数审计：每个隔离参数（--rm/--name/断网/内存/CPU/
  只读根/tmpfs/工作目录）逐一可见；宿主机环境绝不泄漏进容器；卷挂载只读
- execute_in_container 预检：引擎不可用 -> 快速失败 DOCKER_UNAVAILABLE（假引擎注入）
- 危险命令在容器路径同样被拦截（不需要 Docker 在场）
- API：/api/sandbox/docker/status 报告引擎状态；/api/sandbox/docker/execute
  在引擎不可用时返回 503
- 真容器集成（仅当本机 Docker 引擎在运行且镜像可得时执行）：
  容器内真跑命令拿输出、断网验证、只读根验证、超时孤儿容器清理

单元/API 部分全部不依赖 Docker 在场，CI 无引擎也稳定绿。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.tools import sandbox as sandbox_mod
from symbio.tools.sandbox import (
    SandboxExecutor,
    build_docker_run_command,
    check_docker_available,
)

DOCKER_TEST_IMAGE = "python:3.11-slim"


def _docker_ready() -> bool:
    """引擎在跑且测试镜像已在本地（避免测试期间触发拉取）。"""
    try:
        info = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            timeout=10,
        )
        if info.returncode != 0:
            return False
        img = subprocess.run(
            ["docker", "image", "inspect", DOCKER_TEST_IMAGE],
            capture_output=True,
            timeout=10,
        )
        return img.returncode == 0
    except Exception:
        return False


DOCKER_READY = _docker_ready()
requires_docker = pytest.mark.skipif(
    not DOCKER_READY, reason="Docker engine not running or test image missing"
)


# ---------------------------------------------------------------------------
# 单元：docker run 命令构建（无需 Docker）
# ---------------------------------------------------------------------------


def test_docker_run_command_contains_all_isolation_flags():
    cmd = build_docker_run_command("echo hi", "python:3.11-slim", container_name="symbio-sbx-test")
    joined = " ".join(cmd)
    assert cmd[:2] == ["docker", "run"]
    assert "--rm" in cmd
    assert "--name symbio-sbx-test" in joined
    assert "--network none" in joined
    assert "--memory 512m" in joined
    assert "--cpus 1" in joined
    assert "--read-only" in cmd
    assert "/tmp:rw,noexec,nosuid" in joined
    assert "-w /workspace" in joined
    # 镜像后紧跟 sh -c <command>
    idx = cmd.index("python:3.11-slim")
    assert cmd[idx + 1 :] == ["sh", "-c", "echo hi"]


def test_docker_run_command_does_not_leak_host_env(monkeypatch):
    monkeypatch.setenv("PATH", "C:\\Windows\\system32")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    cmd = build_docker_run_command(
        "env", "python:3.11-slim", container_name="n", env={"FOO": "bar"}
    )
    env_args = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-e"]
    assert "FOO=bar" in env_args
    assert "SYMBIO_SANDBOX=1" in env_args
    # 宿主机 PATH / 密钥绝不进容器
    assert not any(e.startswith("PATH=") for e in env_args)
    assert not any("sk-secret" in e for e in env_args)


def test_docker_run_command_mounts_volumes_read_only(tmp_path):
    cmd = build_docker_run_command(
        "ls",
        "python:3.11-slim",
        container_name="n",
        volumes={str(tmp_path): "/workspace"},
    )
    vol_args = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-v"]
    assert len(vol_args) == 1
    assert vol_args[0].endswith("/workspace:ro")


def test_docker_run_command_custom_limits():
    cmd = build_docker_run_command(
        "x",
        "img",
        container_name="n",
        memory_limit="1g",
        cpus="2",
        network="bridge",
        working_dir="/app",
    )
    joined = " ".join(cmd)
    assert "--memory 1g" in joined
    assert "--cpus 2" in joined
    assert "--network bridge" in joined
    assert "-w /app" in joined


# ---------------------------------------------------------------------------
# 单元：执行路径守卫（无需 Docker）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_container_execute_fails_fast_when_engine_unavailable(monkeypatch):
    async def fake_check(timeout: int = 10):
        return False, "engine down (injected)"

    monkeypatch.setattr(sandbox_mod, "check_docker_available", fake_check)
    executor = SandboxExecutor()
    result = await executor.execute_in_container("echo hi")
    assert result.exit_code == -1
    assert result.error_message.startswith("DOCKER_UNAVAILABLE")
    assert "engine down (injected)" in result.error_message
    assert result.metadata["docker_available"] is False


@pytest.mark.asyncio
async def test_container_execute_blocks_dangerous_command():
    executor = SandboxExecutor()
    result = await executor.execute_in_container("rm -rf /")
    assert result.exit_code == -1
    assert result.error_message.startswith("BLOCKED")
    assert result.metadata["mode"] == "docker"


# ---------------------------------------------------------------------------
# API（无需 Docker：不可用路径 + 状态报告）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_docker_status_reports_engine_state(monkeypatch):
    async def fake_check(timeout: int = 10):
        return True, "docker engine 28.0.0 (injected)"

    monkeypatch.setattr(sandbox_mod, "check_docker_available", fake_check)
    from symbio.interfaces.api import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/sandbox/docker/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is True
    assert "injected" in data["detail"]


@pytest.mark.asyncio
async def test_api_docker_execute_returns_503_when_engine_unavailable(monkeypatch):
    async def fake_check(timeout: int = 10):
        return False, "no engine (injected)"

    monkeypatch.setattr(sandbox_mod, "check_docker_available", fake_check)
    from symbio.interfaces.api import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/sandbox/docker/execute", json={"command": "echo hi"})
    assert resp.status_code == 503
    assert "DOCKER_UNAVAILABLE" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 真容器集成（仅本机 Docker 引擎在运行时执行）
# ---------------------------------------------------------------------------


@requires_docker
@pytest.mark.asyncio
async def test_real_container_runs_python_and_captures_output():
    executor = SandboxExecutor()
    result = await executor.execute_in_container(
        'python -c "print(6*7)"', image=DOCKER_TEST_IMAGE, timeout=120
    )
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "42"
    assert result.metadata["mode"] == "docker"
    assert result.metadata["network"] == "none"


@requires_docker
@pytest.mark.asyncio
async def test_real_container_network_is_disabled():
    executor = SandboxExecutor()
    result = await executor.execute_in_container(
        "python -c \"import socket; socket.create_connection(('1.1.1.1', 80), timeout=3)\"",
        image=DOCKER_TEST_IMAGE,
        timeout=120,
    )
    assert result.exit_code != 0  # 断网容器连不出去


@requires_docker
@pytest.mark.asyncio
async def test_real_container_root_filesystem_is_read_only():
    executor = SandboxExecutor()
    result = await executor.execute_in_container(
        "sh -c 'echo x > /etc/hacked'", image=DOCKER_TEST_IMAGE, timeout=120
    )
    assert result.exit_code != 0
    # /tmp 的 tmpfs 仍可写
    result2 = await executor.execute_in_container(
        "sh -c 'echo x > /tmp/ok && cat /tmp/ok'", image=DOCKER_TEST_IMAGE, timeout=120
    )
    assert result2.exit_code == 0
    assert result2.stdout.strip() == "x"


@requires_docker
@pytest.mark.asyncio
async def test_real_container_timeout_cleans_up_orphan():
    executor = SandboxExecutor()
    result = await executor.execute_in_container("sleep 60", image=DOCKER_TEST_IMAGE, timeout=8)
    assert result.timed_out is True
    container_name = result.metadata["container_name"]
    # 兜底清理后容器不应存活
    ps = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name={container_name}", "--format", "{{.Names}}"],
        capture_output=True,
        timeout=15,
        text=True,
    )
    assert container_name not in ps.stdout


@requires_docker
@pytest.mark.asyncio
async def test_real_container_via_api_endpoint():
    from symbio.interfaces.api import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/sandbox/docker/execute",
            json={"command": "echo from-container", "image": DOCKER_TEST_IMAGE, "timeout": 120},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["result"]["stdout"].strip() == "from-container"


@pytest.mark.asyncio
async def test_check_docker_available_returns_tuple():
    available, detail = await check_docker_available(timeout=10)
    assert isinstance(available, bool)
    assert isinstance(detail, str) and detail
