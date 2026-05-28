"""Symbio CLI 入口"""

import typer
from rich.console import Console
from rich.panel import Panel

from symbio import __version__
from symbio.config.settings import get_settings
from symbio.utils.logger import init_logger_from_settings

app = typer.Typer(
    name="symbio",
    help="🧬 Symbio - AI Infra 级多 Agent 协同框架",
    add_completion=False,
)
console = Console()


@app.callback()
def main(
    version: bool = typer.Option(False, "--version", "-v", help="显示版本信息"),
    config: str = typer.Option(None, "--config", "-c", help="配置文件路径"),
):
    """Symbio - AI Infra 级多 Agent 协同框架"""
    if version:
        console.print(f"[bold blue]Symbio[/bold blue] v{__version__}")
        raise typer.Exit()

    # 初始化日志
    init_logger_from_settings()


@app.command()
def init(
    path: str = typer.Argument(".", help="项目路径"),
    name: str = typer.Option("symbio-project", "--name", "-n", help="项目名称"),
):
    """初始化 Symbio 项目"""
    from pathlib import Path

    project_path = Path(path)
    project_path.mkdir(parents=True, exist_ok=True)

    # 创建项目配置
    config_path = project_path / "symbio.yaml"
    settings = get_settings()
    settings.to_yaml(config_path)

    # 创建数据目录
    (project_path / "data" / "lancedb").mkdir(parents=True, exist_ok=True)
    (project_path / "data" / "checkpoints").mkdir(parents=True, exist_ok=True)
    (project_path / "data" / "trajectories").mkdir(parents=True, exist_ok=True)
    (project_path / "logs").mkdir(parents=True, exist_ok=True)

    console.print(Panel(
        f"[bold green]✅ 项目初始化成功！[/bold green]\n\n"
        f"项目名称: {name}\n"
        f"项目路径: {project_path.absolute()}\n"
        f"配置文件: {config_path.absolute()}\n\n"
        f"[bold yellow]下一步:[/bold yellow]\n"
        f"  1. 编辑 {config_path} 配置模型和 API Key\n"
        f"  2. 运行 [bold cyan]symbio chat[/bold cyan] 开始对话",
        title="🧬 Symbio 初始化",
    ))


@app.command()
def chat(
    message: str = typer.Argument(..., help="发送的消息"),
    model: str = typer.Option(None, "--model", "-m", help="指定模型"),
    session: str = typer.Option(None, "--session", "-s", help="会话 ID"),
):
    """与 Agent 对话"""
    import asyncio
    from symbio.core.orchestrator import Orchestrator

    console.print(f"[bold blue]👤 用户:[/bold blue] {message}")

    # TODO: 实现完整的对话流程
    console.print("[bold yellow]⏳ 功能开发中...[/bold yellow]")


@app.command()
def task(
    action: str = typer.Argument(..., help="操作: list/status/cancel"),
    task_id: str = typer.Option(None, "--id", help="任务 ID"),
):
    """任务管理"""
    if action == "list":
        console.print("[bold blue]📋 任务列表[/bold blue]")
        # TODO: 实现任务列表
        console.print("[bold yellow]⏳ 功能开发中...[/bold yellow]")
    elif action == "status":
        if not task_id:
            console.print("[bold red]❌ 请指定任务 ID[/bold red]")
            raise typer.Exit(1)
        console.print(f"[bold blue]📊 任务状态: {task_id}[/bold blue]")
        # TODO: 实现任务状态查询
        console.print("[bold yellow]⏳ 功能开发中...[/bold yellow]")
    elif action == "cancel":
        if not task_id:
            console.print("[bold red]❌ 请指定任务 ID[/bold red]")
            raise typer.Exit(1)
        console.print(f"[bold red]🚫 取消任务: {task_id}[/bold red]")
        # TODO: 实现任务取消
        console.print("[bold yellow]⏳ 功能开发中...[/bold yellow]")


@app.command()
def model(
    action: str = typer.Argument(..., help="操作: list/add/remove/test"),
    name: str = typer.Option(None, "--name", "-n", help="模型名称"),
):
    """模型管理"""
    if action == "list":
        settings = get_settings()
        console.print("[bold blue]🤖 模型列表[/bold blue]")
        console.print(f"  低复杂度: {settings.model.model_low}")
        console.print(f"  中复杂度: {settings.model.model_medium}")
        console.print(f"  高复杂度: {settings.model.model_high}")
    else:
        console.print("[bold yellow]⏳ 功能开发中...[/bold yellow]")


@app.command()
def memory(
    action: str = typer.Argument(..., help="操作: search/list/stats"),
    query: str = typer.Option(None, "--query", "-q", help="搜索查询"),
):
    """记忆管理"""
    if action == "search" and query:
        console.print(f"[bold blue]🔍 搜索记忆: {query}[/bold blue]")
        # TODO: 实现记忆搜索
        console.print("[bold yellow]⏳ 功能开发中...[/bold yellow]")
    elif action == "stats":
        console.print("[bold blue]📊 记忆统计[/bold blue]")
        # TODO: 实现记忆统计
        console.print("[bold yellow]⏳ 功能开发中...[/bold yellow]")
    else:
        console.print("[bold yellow]⏳ 功能开发中...[/bold yellow]")


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", "--host", help="服务主机"),
    port: int = typer.Option(9090, "--port", "-p", help="服务端口"),
    reload: bool = typer.Option(False, "--reload", help="自动重载"),
):
    """启动 Web 服务"""
    console.print(Panel(
        f"[bold green]🚀 启动 Symbio Web 服务[/bold green]\n\n"
        f"地址: http://{host}:{port}\n"
        f"自动重载: {'是' if reload else '否'}\n\n"
        f"[bold yellow]⏳ 功能开发中...[/bold yellow]",
        title="🧬 Symbio Server",
    ))


@app.command()
def eval(
    suite: str = typer.Argument(..., help="评测套件路径"),
    agent: str = typer.Option(None, "--agent", "-a", help="指定 Agent"),
):
    """运行评测"""
    console.print(f"[bold blue]🧪 运行评测: {suite}[/bold blue]")
    # TODO: 实现评测管道
    console.print("[bold yellow]⏳ 功能开发中...[/bold yellow]")


@app.command()
def export(
    format: str = typer.Option("sharegpt", "--format", "-f", help="导出格式"),
    output: str = typer.Option(None, "--output", "-o", help="输出路径"),
):
    """导出微调数据集"""
    console.print(f"[bold blue]📦 导出数据集: {format}[/bold blue]")
    # TODO: 实现数据集导出
    console.print("[bold yellow]⏳ 功能开发中...[/bold yellow]")


if __name__ == "__main__":
    app()
