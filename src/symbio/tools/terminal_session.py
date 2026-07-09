"""交互式 PTY 终端会话 —— 在网页里起一个真终端跑 claude-code / codex / shell。

与 external_agents（一次性 `-p` 调用）和 external_live_session（tail 转录文件）不同，
这里分配一个真正的伪终端（Windows 用 winpty，POSIX 用内置 pty），把子进程的
stdin/stdout 双向接到 WebSocket，前端用 xterm.js 渲染。这样 claude/codex 的
交互式 TUI（审批提示、进度、颜色）都能正常工作。

设计要点：
- PTY 的 read() 是阻塞的，用一个后台读线程把输出灌进 asyncio 队列，避免堵事件循环
- 子进程环境剥离宿主 ANTHROPIC_*（复用 external_agents 的清理），防 401
- 断连/退出时确保杀掉子进程与读线程，不留孤儿
"""

from __future__ import annotations

import asyncio
import os
import shutil
import threading
from typing import Optional

from symbio.tools.external_agents import _clean_subprocess_env
from symbio.utils.logger import get_logger

logger = get_logger("tools.terminal_session")

# 允许在终端里起的目标：claude-code / codex / shell。裸 shell 完全不设防，
# 由「默认只听本机」的 WS 鉴权兜底。
_SHELL_DEFAULT = "powershell.exe" if os.name == "nt" else (os.environ.get("SHELL") or "/bin/bash")


def resolve_terminal_command(kind: str) -> list[str]:
    """把终端类型解析成可执行命令（含 Windows .CMD 完整路径）。

    kind: "claude-code" | "codex" | "shell"
    找不到对应 CLI 时抛 FileNotFoundError。
    """
    normalized = (kind or "shell").strip().lower()
    if normalized in ("shell", "", "cmd", "powershell", "bash"):
        return [_SHELL_DEFAULT]

    exe_name = {"claude-code": "claude", "claude": "claude", "codex": "codex"}.get(normalized)
    if not exe_name:
        raise ValueError(f"Unsupported terminal kind: {kind}")
    path = shutil.which(exe_name)
    if not path:
        raise FileNotFoundError(f"{exe_name} CLI not found on PATH")
    return [path]


class TerminalSession:
    """一个 PTY 支撑的交互终端会话。跨平台封装 winpty / pty。"""

    def __init__(self, command: list[str], *, cwd: Optional[str] = None,
                 cols: int = 100, rows: int = 30) -> None:
        self.command = command
        self.cwd = cwd or os.getcwd()
        self.cols = cols
        self.rows = rows
        self._proc = None
        self._reader: Optional[threading.Thread] = None
        self._alive = False

    def start(self) -> None:
        """分配 PTY 并起子进程。"""
        env = _clean_subprocess_env()
        # PTY 里跑交互 CLI 需要 UTF-8，否则中文/表情/框线乱码
        env.setdefault("PYTHONIOENCODING", "utf-8")
        if os.name == "nt":
            from winpty import PtyProcess

            # winpty 接受字符串命令行；用 subprocess.list2cmdline 正确加引号
            import subprocess

            cmdline = subprocess.list2cmdline(self.command)
            self._proc = PtyProcess.spawn(
                cmdline, cwd=self.cwd, env=env,
                dimensions=(self.rows, self.cols),
            )
        else:
            from ptyprocess import PtyProcess  # type: ignore

            self._proc = PtyProcess.spawn(
                self.command, cwd=self.cwd, env=env,
                dimensions=(self.rows, self.cols),
            )
        self._alive = True

    def write(self, data: str) -> None:
        """把键盘输入写进 PTY。"""
        if self._proc is not None and self._alive:
            try:
                self._proc.write(data)
            except (EOFError, OSError):
                self._alive = False

    def resize(self, cols: int, rows: int) -> None:
        """前端终端尺寸变化时同步给 PTY（否则 TUI 布局会错）。"""
        self.cols, self.rows = cols, rows
        if self._proc is not None and self._alive:
            try:
                self._proc.setwinsize(rows, cols)
            except Exception:
                pass

    def is_alive(self) -> bool:
        if self._proc is None:
            return False
        try:
            return bool(self._proc.isalive())
        except Exception:
            return False

    def terminate(self) -> None:
        """杀掉子进程 + 停读线程，断连时必须调用，防孤儿。"""
        self._alive = False
        if self._proc is not None:
            try:
                self._proc.terminate(force=True) if os.name != "nt" else self._proc.terminate()
            except Exception:
                try:
                    self._proc.kill(1)
                except Exception:
                    pass
        self._proc = None

    def start_reader(self, loop: asyncio.AbstractEventLoop, queue: "asyncio.Queue[Optional[str]]") -> None:
        """起后台线程阻塞读 PTY 输出，把每块灌进 asyncio 队列；EOF 投 None。"""

        def _pump() -> None:
            while self._alive:
                try:
                    data = self._proc.read(4096)
                except EOFError:
                    break
                except Exception:
                    break
                if data:
                    loop.call_soon_threadsafe(queue.put_nowait, data)
                else:
                    break
            self._alive = False
            loop.call_soon_threadsafe(queue.put_nowait, None)  # 结束信号

        self._reader = threading.Thread(target=_pump, name="terminal-pty-reader", daemon=True)
        self._reader.start()
