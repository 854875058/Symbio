"""交互式 PTY 终端会话测试。

纯逻辑测试（命令解析）无条件跑；真实 PTY 往返测试在 winpty/pty 不可用时跳过，
保证无 PTY 的 CI 环境仍全绿。
"""

from __future__ import annotations

import asyncio
import os
import shutil

import pytest

from symbio.tools.terminal_session import (
    TerminalSession,
    resolve_terminal_command,
    _SHELL_DEFAULT,
)

# 真实 PTY 是否可用（Windows 需 winpty，POSIX 需 ptyprocess）
try:
    if os.name == "nt":
        import winpty  # noqa: F401
    else:
        import ptyprocess  # noqa: F401
    _PTY_READY = True
except Exception:
    _PTY_READY = False

requires_pty = pytest.mark.skipif(
    not _PTY_READY, reason="PTY backend (winpty/ptyprocess) not available"
)


# ---------- 命令解析（纯逻辑，无条件跑）----------


def test_shell_kind_resolves_to_default_shell():
    assert resolve_terminal_command("shell") == [_SHELL_DEFAULT]
    assert resolve_terminal_command("") == [_SHELL_DEFAULT]
    assert resolve_terminal_command("cmd") == [_SHELL_DEFAULT]


def test_unsupported_kind_raises_value_error():
    with pytest.raises(ValueError):
        resolve_terminal_command("rm-rf-everything")


def test_claude_and_codex_resolve_to_full_path_or_missing():
    # 装了就应解析出完整路径，没装则抛 FileNotFoundError（不静默返回裸名）
    for kind, exe in [("claude-code", "claude"), ("codex", "codex")]:
        if shutil.which(exe):
            resolved = resolve_terminal_command(kind)
            assert len(resolved) == 1
            assert resolved[0] == shutil.which(exe)
        else:
            with pytest.raises(FileNotFoundError):
                resolve_terminal_command(kind)


def test_claude_alias_maps_to_claude():
    if shutil.which("claude"):
        assert resolve_terminal_command("claude") == resolve_terminal_command("claude-code")


def test_resume_id_builds_interactive_resume_command():
    """接管：resume_id 非空时应构造交互式续接命令。"""
    if shutil.which("claude"):
        cmd = resolve_terminal_command("claude-code", resume_id="sess-abc")
        # claude 交互式 resume：--resume <id>，不带 -p/--print
        assert cmd[-2:] == ["--resume", "sess-abc"]
        assert "-p" not in cmd and "--print" not in cmd
    if shutil.which("codex"):
        cmd = resolve_terminal_command("codex", resume_id="sess-xyz")
        # codex 顶层交互式 resume 子命令（非 exec resume）
        assert cmd[-2:] == ["resume", "sess-xyz"]
        assert "exec" not in cmd


def test_shell_ignores_resume_id():
    assert resolve_terminal_command("shell", resume_id="whatever") == [_SHELL_DEFAULT]


def test_empty_resume_id_stays_fresh():
    if shutil.which("claude"):
        assert resolve_terminal_command("claude-code", resume_id="") == resolve_terminal_command(
            "claude-code"
        )
        assert resolve_terminal_command("claude-code", resume_id="   ") == resolve_terminal_command(
            "claude-code"
        )


# ---------- 真实 PTY 往返（需要 PTY 后端）----------


@requires_pty
def test_pty_roundtrip_captures_command_output():
    """起 PTY 跑 echo，应能读回带标记的输出，并检测到进程退出。"""

    async def run() -> str:
        if os.name == "nt":
            command = ["cmd", "/c", "echo PTY-ROUNDTRIP-42"]
        else:
            command = ["/bin/sh", "-c", "echo PTY-ROUNDTRIP-42"]
        session = TerminalSession(command, cols=80, rows=24)
        session.start()
        loop = asyncio.get_event_loop()
        queue: "asyncio.Queue[str | None]" = asyncio.Queue()
        session.start_reader(loop, queue)
        chunks: list[str] = []
        while True:
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=6)
            except asyncio.TimeoutError:
                break
            if chunk is None:  # EOF
                break
            chunks.append(chunk)
        session.terminate()
        return "".join(chunks)

    output = asyncio.run(run())
    assert "PTY-ROUNDTRIP-42" in output


@requires_pty
def test_pty_write_feeds_stdin():
    """向 PTY 写入命令，应在输出里看到其结果。"""

    async def run() -> str:
        session = TerminalSession([_SHELL_DEFAULT], cols=90, rows=25)
        session.start()
        loop = asyncio.get_event_loop()
        queue: "asyncio.Queue[str | None]" = asyncio.Queue()
        session.start_reader(loop, queue)
        await asyncio.sleep(1.0)  # 等 shell 起来
        session.write("echo PTY-STDIN-OK\r\n")
        chunks: list[str] = []
        deadline = asyncio.get_event_loop().time() + 8
        while asyncio.get_event_loop().time() < deadline:
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=2)
            except asyncio.TimeoutError:
                continue
            if chunk is None:
                break
            chunks.append(chunk)
            if "PTY-STDIN-OK" in "".join(chunks):
                break
        session.terminate()
        return "".join(chunks)

    output = asyncio.run(run())
    assert "PTY-STDIN-OK" in output


@requires_pty
def test_terminate_stops_the_process():
    """terminate 后 is_alive 应为 False。"""
    session = TerminalSession([_SHELL_DEFAULT], cols=80, rows=24)
    session.start()
    assert session.is_alive() is True
    session.terminate()
    assert session.is_alive() is False


@requires_pty
def test_resize_does_not_raise():
    session = TerminalSession([_SHELL_DEFAULT], cols=80, rows=24)
    session.start()
    try:
        session.resize(120, 40)  # 不应抛
        assert session.cols == 120 and session.rows == 40
    finally:
        session.terminate()
