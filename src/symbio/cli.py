"""Symbio command line interface."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from symbio import __version__
from symbio.config.settings import get_settings
from symbio.interfaces.database import Database, DEFAULT_DB_PATH
from symbio.utils.logger import setup_logger

app = typer.Typer(
    name="symbio",
    help="Symbio - AI Infra multi-agent orchestration framework",
    add_completion=False,
)
console = Console()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", "-v", help="Show version"),
    config: Optional[str] = typer.Option(None, "--config", "-c", help="Config file path"),
) -> None:
    """Symbio CLI."""
    if version:
        console.print(f"[bold blue]Symbio[/bold blue] v{__version__}")
        raise typer.Exit()

    setup_logger(level="WARNING")

    if config:
        ctx.obj = {"config": config}
        # Ensure get_settings() picks up the explicit config file
        import os

        os.environ["SYMBIO_CONFIG_FILE"] = config

    if ctx.invoked_subcommand is None:
        # 裸跑 `symbio` 时读配置层，而不是硬编码 0.0.0.0：API 没有默认鉴权，
        # 绑到所有网卡等于把沙箱执行和 PTY 终端暴露给整个局域网。
        host, port = _configured_bind()
        _start_web(host=host, port=port, reload=False)


_PUBLIC_BINDS = {"0.0.0.0", "::", ""}


def _configured_bind() -> tuple[str, int]:
    """从配置层读取监听地址，失败时退回仅本机。"""
    try:
        from symbio.config.settings import get_settings

        server = get_settings().server
        return str(server.host), int(server.port)
    except Exception:
        return "127.0.0.1", 9090


def _auth_token_configured() -> bool:
    """是否已配置全局 API token（配置层或环境变量）。"""
    import os

    if os.environ.get("SYMBIO_API_TOKEN", "").strip():
        return True
    try:
        from symbio.config.settings import get_settings

        return bool(str(getattr(get_settings().server, "api_token", "")).strip())
    except Exception:
        return False


def _run(coro):
    return asyncio.run(coro)


async def _with_db(fn):
    db = Database(DEFAULT_DB_PATH)
    await db.connect()
    try:
        return await fn(db)
    finally:
        await db.close()


def _clip(value: Any, limit: int = 80) -> str:
    text = "" if value is None else str(value).replace("\n", " ")
    return text if len(text) <= limit else text[: max(limit - 3, 0)] + "..."


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


async def _ensure_session(db: Database, session_id: str, title: str) -> None:
    if not await db.get_session(session_id):
        await db.create_session(session_id, title=title)


def _render_tasks(tasks: list[dict]) -> None:
    table = Table(title="Tasks")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Agent", no_wrap=True)
    table.add_column("Name")
    table.add_column("Updated", no_wrap=True)
    for task in tasks:
        table.add_row(
            task["id"],
            task["status"],
            task.get("agent") or "-",
            _clip(task.get("name"), 48),
            task.get("completed_at") or task.get("created_at") or "",
        )
    console.print(table)


def _render_memories(memories: list[dict]) -> None:
    table = Table(title="Memories")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Importance", justify="right", no_wrap=True)
    table.add_column("Tags")
    table.add_column("Title")
    table.add_column("Content")
    for mem in memories:
        table.add_row(
            mem["id"],
            f"{float(mem.get('importance', 0.0)):.2f}",
            ", ".join(mem.get("tags", [])),
            _clip(mem.get("title"), 32),
            _clip(mem.get("content"), 72),
        )
    console.print(table)


async def _find_model(db: Database, model_ref: str) -> Optional[dict]:
    direct = await db.get_model(model_ref)
    if direct:
        return direct
    for item in await db.list_models():
        if model_ref in {item["id"], item["model_id"], item.get("display_name")}:
            return item
    return None


@app.command()
def init(
    path: str = typer.Argument(".", help="Project path"),
    name: str = typer.Option("symbio-project", "--name", "-n", help="Project name"),
) -> None:
    """Initialize a Symbio project."""
    project_path = Path(path)
    project_path.mkdir(parents=True, exist_ok=True)

    config_path = project_path / "symbio.yaml"
    settings = get_settings()
    settings.to_yaml(config_path)

    (project_path / "data" / "lancedb").mkdir(parents=True, exist_ok=True)
    (project_path / "data" / "checkpoints").mkdir(parents=True, exist_ok=True)
    (project_path / "data" / "trajectories").mkdir(parents=True, exist_ok=True)
    (project_path / "logs").mkdir(parents=True, exist_ok=True)

    console.print(
        Panel(
            f"[bold green]Project initialized[/bold green]\n\n"
            f"Name: {name}\n"
            f"Path: {project_path.absolute()}\n"
            f"Config: {config_path.absolute()}\n\n"
            'Next: edit symbio.yaml, then run `symbio chat "hello"`.',
            title="Symbio Init",
        )
    )


@app.command()
def chat(
    message: str = typer.Argument(..., help="Message to send"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override model name"),
    session: Optional[str] = typer.Option(None, "--session", "-s", help="Session ID"),
) -> None:
    """Send a message through the orchestrator and persist the conversation."""

    async def run_chat(db: Database) -> int:
        from symbio.core.orchestrator import Orchestrator
        from symbio.utils.types import Message, MessageSource

        session_id = session or "cli"
        await _ensure_session(db, session_id, "CLI Session")

        now = _now()
        await db.create_message(f"msg-{uuid.uuid4().hex[:12]}", session_id, "user", message, now, 0)
        task = await db.create_task(
            task_id=f"task-{uuid.uuid4().hex[:10]}",
            name=_clip(message, 64),
            status="running",
            agent="orchestrator",
            description=message,
        )
        step = await db.create_task_step(task["id"], "orchestrator.process", "running")

        msg = Message(
            source=MessageSource.CLI,
            user_id="local",
            content=message,
            session_id=session_id,
            metadata={"model_override": model} if model else {},
        )

        console.print(f"[bold blue]User:[/bold blue] {message}")
        try:
            result = await Orchestrator().process(msg)
            status = "completed" if result.success else "failed"
            await db.update_task_step_status(step["id"], status)
            await db.update_task_status(task["id"], status, result.content)
            await db.create_message(
                f"msg-{uuid.uuid4().hex[:12]}",
                session_id,
                "assistant",
                result.content,
                _now(),
                result.token_usage.total_tokens,
            )
            console.print(f"[bold green]Symbio:[/bold green] {result.content}")
            console.print(f"[dim]task_id={task['id']} session={session_id} status={status}[/dim]")
            return 0 if result.success else 1
        except Exception as exc:
            error = str(exc)
            await db.update_task_step_status(step["id"], "failed")
            await db.update_task_status(task["id"], "failed", error)
            await db.create_message(
                f"msg-{uuid.uuid4().hex[:12]}", session_id, "assistant", error, _now(), 0
            )
            console.print(f"[bold red]Error:[/bold red] {error}")
            return 1

    code = _run(_with_db(run_chat))
    if code:
        raise typer.Exit(code)


@app.command()
def task(
    action: str = typer.Argument(..., help="Action: list/status/cancel"),
    task_id: Optional[str] = typer.Option(None, "--id", help="Task ID"),
    status: Optional[str] = typer.Option(None, "--status", help="Filter by status"),
) -> None:
    """Manage persisted tasks."""

    async def run_task(db: Database) -> int:
        if action == "list":
            tasks = await db.list_tasks(status=status)
            _render_tasks(tasks)
            return 0

        if action == "status":
            if not task_id:
                console.print("[bold red]Missing --id[/bold red]")
                return 1
            item = await db.get_task(task_id)
            if not item:
                console.print(f"[bold red]Task not found:[/bold red] {task_id}")
                return 1
            _render_tasks([item])
            if item.get("steps"):
                steps = Table(title=f"Steps for {task_id}")
                steps.add_column("ID", no_wrap=True)
                steps.add_column("Status", no_wrap=True)
                steps.add_column("Name")
                steps.add_column("Duration", no_wrap=True)
                for step in item["steps"]:
                    steps.add_row(
                        str(step["id"]), step["status"], step["name"], step.get("duration") or ""
                    )
                console.print(steps)
            if item.get("result"):
                console.print(Panel(_clip(item["result"], 1200), title="Result"))
            return 0

        if action == "cancel":
            if not task_id:
                console.print("[bold red]Missing --id[/bold red]")
                return 1
            updated = await db.update_task_status(task_id, "failed", "Cancelled from CLI")
            if not updated:
                console.print(f"[bold red]Task not found:[/bold red] {task_id}")
                return 1
            console.print(f"[bold green]Cancelled:[/bold green] {task_id}")
            return 0

        console.print("[bold red]Unknown action. Use list/status/cancel.[/bold red]")
        return 1

    code = _run(_with_db(run_task))
    if code:
        raise typer.Exit(code)


@app.command()
def model(
    action: str = typer.Argument(..., help="Action: list/add/remove/test"),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Model name"),
    provider: str = typer.Option("anthropic", "--provider", help="Provider"),
    display_name: str = typer.Option("", "--display-name", help="Display name"),
) -> None:
    """Manage configured models."""

    async def run_model(db: Database) -> int:
        if action == "list":
            models = await db.list_models()
            table = Table(title="Models")
            table.add_column("Record ID", style="cyan")
            table.add_column("Model ID")
            table.add_column("Provider")
            table.add_column("Enabled")
            table.add_column("Display Name")
            for item in models:
                table.add_row(
                    item["id"],
                    item["model_id"],
                    item["provider"],
                    str(item["enabled"]),
                    item.get("display_name") or "",
                )
            console.print(table)
            return 0

        if action == "add":
            if not name:
                console.print("[bold red]Missing --name[/bold red]")
                return 1
            await db.create_model(name, provider=provider, display_name=display_name or name)
            console.print(f"[bold green]Model added:[/bold green] {name}")
            return 0

        if action == "remove":
            if not name:
                console.print("[bold red]Missing --name[/bold red]")
                return 1
            item = await _find_model(db, name)
            ok = await db.delete_model(item["id"]) if item else False
            console.print(
                f"[bold green]Removed:[/bold green] {name}"
                if ok
                else f"[bold red]Not found:[/bold red] {name}"
            )
            return 0 if ok else 1

        if action == "test":
            if not name:
                console.print("[bold red]Missing --name[/bold red]")
                return 1
            item = await _find_model(db, name)
            if not item:
                console.print(f"[bold red]Model not found:[/bold red] {name}")
                return 1
            console.print(
                Panel(json.dumps(item, ensure_ascii=False, indent=2), title="Model Config")
            )
            return 0

        console.print("[bold red]Unknown action. Use list/add/remove/test.[/bold red]")
        return 1

    code = _run(_with_db(run_model))
    if code:
        raise typer.Exit(code)


@app.command()
def memory(
    action: str = typer.Argument(..., help="Action: search/list/stats/store"),
    query: Optional[str] = typer.Option(None, "--query", "-q", help="Search query"),
    title: str = typer.Option("", "--title", help="Memory title"),
    content: str = typer.Option("", "--content", help="Memory content"),
    tag: list[str] = typer.Option([], "--tag", help="Memory tag"),
) -> None:
    """Manage memories."""

    async def run_memory(db: Database) -> int:
        if action == "search":
            memories = await db.search_memories(query or "")
            _render_memories(memories)
            return 0

        if action == "list":
            memories = await db.list_memories()
            _render_memories(memories)
            return 0

        if action == "stats":
            memories = await db.list_memories()
            avg = (
                sum(float(m.get("importance", 0.0)) for m in memories) / len(memories)
                if memories
                else 0.0
            )
            table = Table(title="Memory Stats")
            table.add_column("Metric")
            table.add_column("Value", justify="right")
            table.add_row("total", str(len(memories)))
            table.add_row("avg_importance", f"{avg:.3f}")
            table.add_row("sources", str(len({m.get("source") for m in memories})))
            console.print(table)
            return 0

        if action == "store":
            if not content:
                console.print("[bold red]Missing --content[/bold red]")
                return 1
            item = await db.create_memory(
                memory_id=f"mem-{uuid.uuid4().hex[:10]}",
                title=title or _clip(content, 48),
                content=content,
                tags=list(tag),
                source="cli",
            )
            console.print(f"[bold green]Memory stored:[/bold green] {item['id']}")
            return 0

        console.print("[bold red]Unknown action. Use search/list/stats/store.[/bold red]")
        return 1

    code = _run(_with_db(run_memory))
    if code:
        raise typer.Exit(code)


def _render_project_memories(items: list[Any]) -> None:
    table = Table(title="Project Memories")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Type")
    table.add_column("Importance", justify="right")
    table.add_column("Content")
    table.add_column("Tags")
    for item in items:
        table.add_row(
            _clip(item.memory_id, 14),
            item.memory_type,
            f"{item.importance:.2f}",
            _clip(item.content, 46),
            ",".join(item.tags),
        )
    console.print(table)


@app.command()
def project(
    action: str = typer.Argument(
        ...,
        help="Action: create/list/add/search/memories/transfer/stats",
    ),
    project_id: str = typer.Option("", "--project", "-p", help="Project ID"),
    name: str = typer.Option("", "--name", help="Project name (create)"),
    description: str = typer.Option("", "--description", help="Project description (create)"),
    content: str = typer.Option("", "--content", help="Memory content (add)"),
    memory_type: str = typer.Option("semantic", "--type", help="Memory type (add)"),
    importance: float = typer.Option(0.5, "--importance", help="Memory importance (add)"),
    query: Optional[str] = typer.Option(None, "--query", "-q", help="Search query"),
    target: str = typer.Option("", "--target", help="Target project ID (transfer)"),
    memory_id: list[str] = typer.Option([], "--memory-id", help="Memory ID (transfer)"),
    reason: str = typer.Option("", "--reason", help="Transfer reason"),
    tag: list[str] = typer.Option([], "--tag", help="Tag"),
) -> None:
    """Manage project-scoped memories and cross-project knowledge transfer."""
    from symbio.memory import ProjectMemoryManager

    async def run_project() -> int:
        manager = ProjectMemoryManager()
        await manager.initialize()
        try:
            if action == "create":
                if not project_id:
                    console.print("[bold red]Missing --project[/bold red]")
                    return 1
                scope = await manager.create_project_async(
                    project_id,
                    project_name=name,
                    description=description,
                    tags=list(tag),
                )
                console.print(f"[bold green]Project created:[/bold green] {scope.project_id}")
                return 0

            if action == "list":
                table = Table(title="Projects")
                table.add_column("ID", style="cyan")
                table.add_column("Name")
                table.add_column("Memories", justify="right")
                for scope in manager.list_projects():
                    count = len(await manager.list_memories(scope.project_id))
                    table.add_row(scope.project_id, scope.project_name or "-", str(count))
                console.print(table)
                return 0

            if action == "add":
                if not project_id or not content:
                    console.print("[bold red]Missing --project or --content[/bold red]")
                    return 1
                if manager.get_project(project_id) is None:
                    manager.create_project(project_id)
                item = await manager.add_memory(
                    project_id,
                    content,
                    memory_type,
                    importance=importance,
                    tags=list(tag),
                    source="cli",
                )
                console.print(f"[bold green]Memory added:[/bold green] {item.memory_id}")
                return 0

            if action == "memories":
                if not project_id:
                    console.print("[bold red]Missing --project[/bold red]")
                    return 1
                _render_project_memories(
                    await manager.list_memories(project_id, tags=list(tag) or None)
                )
                return 0

            if action == "search":
                if not query:
                    console.print("[bold red]Missing --query[/bold red]")
                    return 1
                results = await manager.search(query, project_id=project_id or None)
                table = Table(title="Search Results")
                table.add_column("Project", style="cyan")
                table.add_column("Score", justify="right")
                table.add_column("Content")
                for row in results:
                    table.add_row(
                        row.get("project_id", "-"),
                        f"{float(row.get('similarity', 0.0)):.3f}",
                        _clip(row.get("content", ""), 56),
                    )
                console.print(table)
                return 0

            if action == "transfer":
                if not project_id or not target or not memory_id:
                    console.print(
                        "[bold red]Need --project, --target and at least one --memory-id[/bold red]"
                    )
                    return 1
                record = await manager.transfer_knowledge(
                    project_id, target, list(memory_id), reason=reason
                )
                console.print(
                    f"[bold green]Transferred:[/bold green] "
                    f"{len(record.memory_ids)} memories {project_id} -> {target}"
                )
                return 0

            if action == "stats":
                stats = manager.get_statistics()
                table = Table(title="Project Memory Stats")
                table.add_column("Metric")
                table.add_column("Value", justify="right")
                for key, value in stats.items():
                    table.add_row(key, _clip(value, 40))
                console.print(table)
                return 0

            console.print(
                "[bold red]Unknown action. "
                "Use create/list/add/search/memories/transfer/stats.[/bold red]"
            )
            return 1
        finally:
            await manager.close()

    code = _run(run_project())
    if code:
        raise typer.Exit(code)


@app.command()
def serve(
    host: Optional[str] = typer.Option(
        None, "--host", help="Bind address (default: config, else 127.0.0.1)"
    ),
    port: Optional[int] = typer.Option(None, "--port", "-p", help="Port"),
    reload: bool = typer.Option(False, "--reload", help="Reload on changes"),
) -> None:
    """Start the FastAPI web service (default when no command is given)."""
    default_host, default_port = _configured_bind()
    _start_web(
        host=host if host is not None else default_host,
        port=port if port is not None else default_port,
        reload=reload,
    )


def _start_web(host: str, port: int, reload: bool) -> None:
    """Launch the FastAPI web service and print a clickable local address."""
    import uvicorn

    # Bind address may be 0.0.0.0 (all interfaces) but that is not a usable URL
    # to click. Show a reachable local address while still binding as requested.
    display_host = "127.0.0.1" if host in _PUBLIC_BINDS else host
    lines = [
        "[bold green]Starting Symbio Web[/bold green]\n",
        f"API: http://{display_host}:{port}",
        f"UI:  http://{display_host}:{port}/ui",
    ]
    if display_host != host:
        lines.append(f"\n[dim]Bound to {host} — also reachable on your LAN.[/dim]")
    console.print(Panel("\n".join(lines), title="Symbio Server"))

    # 对外绑定 + 无鉴权 = 局域网内任何人都能调沙箱执行和 PTY 终端。必须显式告警。
    if host in _PUBLIC_BINDS and not _auth_token_configured():
        console.print(
            Panel(
                "[bold red]警告：正在监听所有网卡，且 API 未配置鉴权。[/bold red]\n\n"
                "同网段内任何人都可调用沙箱执行、PTY 终端和配置接口。\n"
                "请设置 [bold]SYMBIO_API_TOKEN[/bold]（或 server.api_token），"
                "或改用 [bold]--host 127.0.0.1[/bold]。",
                title="[bold red]Security[/bold red]",
                border_style="red",
            )
        )

    uvicorn.run("symbio.interfaces.api:app", host=host, port=port, reload=reload)


@app.command()
def eval(
    suite: str = typer.Argument(..., help="Evaluation suite path"),
    agent: Optional[str] = typer.Option(None, "--agent", "-a", help="Agent name"),
) -> None:
    """Run an evaluation suite when the eval pipeline is available."""
    suite_path = Path(suite)
    if not suite_path.exists():
        console.print(f"[bold red]Suite not found:[/bold red] {suite}")
        raise typer.Exit(1)
    console.print(
        Panel(
            f"Suite: {suite_path}\nAgent: {agent or 'default'}\n"
            "Use the Python EvalPipeline API for custom executors.",
            title="Eval",
        )
    )


@app.command()
def export(
    format: str = typer.Option("sharegpt", "--format", "-f", help="sharegpt/alpaca/openai/raw"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output JSONL path"),
    session: Optional[str] = typer.Option(None, "--session", "-s", help="Only export one session"),
) -> None:
    """Export persisted conversations as fine-tuning JSONL."""

    async def run_export(db: Database) -> int:
        output_path = Path(output or f"data/exports/symbio_{format}_{int(time.time())}.jsonl")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        sessions = [await db.get_session(session)] if session else await db.list_sessions()
        sessions = [s for s in sessions if s]
        count = 0
        with output_path.open("w", encoding="utf-8") as fh:
            for sess in sessions:
                messages = await db.list_messages_by_session(sess["id"])
                messages = [m for m in messages if m["role"] in {"user", "assistant", "system"}]
                if len(messages) < 2:
                    continue
                sample = _format_export_sample(format, sess["id"], messages)
                fh.write(json.dumps(sample, ensure_ascii=False) + "\n")
                count += 1

        console.print(f"[bold green]Exported {count} samples:[/bold green] {output_path}")
        return 0

    code = _run(_with_db(run_export))
    if code:
        raise typer.Exit(code)


def _format_export_sample(format_name: str, session_id: str, messages: list[dict]) -> dict:
    normalized = [{"role": m["role"], "content": m["content"]} for m in messages]
    fmt = format_name.lower()
    if fmt == "sharegpt":
        role_map = {"user": "human", "assistant": "gpt", "system": "system"}
        return {
            "id": session_id,
            "conversations": [
                {"role": role_map.get(m["role"], m["role"]), "content": m["content"]}
                for m in normalized
            ],
        }
    if fmt == "alpaca":
        first_user = next((m["content"] for m in normalized if m["role"] == "user"), "")
        last_assistant = next(
            (m["content"] for m in reversed(normalized) if m["role"] == "assistant"), ""
        )
        return {"id": session_id, "instruction": first_user, "input": "", "output": last_assistant}
    if fmt == "openai":
        return {"id": session_id, "messages": normalized}
    return {"id": session_id, "messages": normalized}


if __name__ == "__main__":
    app()
