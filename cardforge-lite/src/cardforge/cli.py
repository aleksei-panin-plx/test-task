# ruff: noqa: E501
"""`cardforge` command of CardForge Light: doctor, the MCP server and an offline plan."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from cardforge.orchestrator import run
from cardforge.orchestrator.workspace import Workspace

app = typer.Typer(no_args_is_help=True, add_completion=False, help="CardForge Light: план коллекции (G1) и таблицы контента")
ROOT = typer.Option(Path("."), "--root", help="Папка CardForge Light")


@app.command()
def doctor(root: Path = ROOT, as_json: bool = typer.Option(False, "--json")) -> None:
    """Проверить установку: знания, политики, инструкция планировщика, папка планов."""
    from cardforge.doctor import collect

    report = collect(root)
    if as_json:
        typer.echo(report.model_dump_json(indent=2))
    else:
        typer.echo(f"CardForge Light {report.versions.get('cardforge-lite') or ''} · {report.platform} · Python {report.python} · {report.mode}")
        for c in report.checks:
            typer.echo(f"[{c.status.upper()}] {c.name}: {c.detail}")
    raise typer.Exit(1 if report.has_errors else 0)


@app.command()
def mcp(root: Path = ROOT) -> None:
    """MCP-сервер по stdio (его запускают Claude Code и Codex)."""
    from cardforge.mcp.server import serve_stdio

    serve_stdio(Workspace.from_env(root))


@app.command()
def plan(
    collection: str = typer.Argument(..., help="Тема коллекции («Лето»)"),
    sets: int | None = typer.Option(None, "--sets", min=1, max=16, help="Сколько сетов (по умолчанию 16)"),
    wishes: str = typer.Option("", "--wishes"),
    markets: list[str] = typer.Option([], "--market"),
    fake: bool = typer.Option(False, "--fake", help="Офлайн-планировщик FakeLLM: dry-run без агента"),
    root: Path = ROOT,
) -> None:
    """Составить план. С --fake — сразу до таблиц (проверка правок знаний); без него — задачи для агента."""
    ws = Workspace.from_env(root)
    rid = run.create(ws, collection, sets=sets, wishes=wishes, markets=markets, llm="fake" if fake else "host-agent")
    out = run.advance(ws, rid)
    if out["next"] == "show_plan":
        typer.echo(run.plan_view(ws, rid)["markdown"])
    typer.echo(json.dumps(out, ensure_ascii=False, indent=1))
    raise typer.Exit(1 if out["state"] == "failed" else 0)


@app.command()
def show(run_id: str, root: Path = ROOT) -> None:
    """Показать таблицы плана прогона (Markdown) и пути к CSV/JSON."""
    ws = Workspace.from_env(root)
    view = run.plan_view(ws, run_id)
    typer.echo(view["markdown"])
    typer.echo(json.dumps(view["files"], ensure_ascii=False, indent=1))


if __name__ == "__main__":
    app()
