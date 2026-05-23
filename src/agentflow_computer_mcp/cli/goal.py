"""`agentflow goal ...` — cross-device goals via REST."""
from __future__ import annotations

import typer

from . import rest_client
from .format import table

app = typer.Typer(help="Цели агентов (cross-device).", no_args_is_help=True)


def _server_or_exit(fn):
    try:
        return fn()
    except rest_client.NotAuthenticated as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=4) from exc
    except rest_client.ServerError as exc:
        typer.echo(f"server: {exc}", err=True)
        raise typer.Exit(code=6) from exc


def _coerce_list(data, key: str) -> list[dict]:
    if data is None:
        return []
    if isinstance(data, dict):
        inner = data.get(key) or data.get("items") or data.get("results") or []
        return [x for x in inner if isinstance(x, dict)]
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []


@app.command("list")
def list_() -> None:
    """Список целей."""
    data = _server_or_exit(lambda: rest_client.get("/me/autonomous/goals"))
    goals = _coerce_list(data, "goals")
    rows = [
        {
            "id": g.get("id", ""),
            "title": (g.get("title") or "")[:50],
            "metric": g.get("metric", "") or "",
            "target": g.get("target", "") or "",
            "status": g.get("status", "") or "",
        }
        for g in goals
    ]
    typer.echo(table(rows, ["id", "title", "metric", "target", "status"]))


@app.command("create")
def create(
    title: str = typer.Argument(..., help="Заголовок цели"),
    metric: str | None = typer.Option(None, "--metric", help="Метрика (revenue, signups, ...)"),
    target: float | None = typer.Option(None, "--target", help="Целевое число"),
    deadline: str | None = typer.Option(None, "--deadline", help="ISO дата"),
) -> None:
    """Создать новую цель."""
    body: dict[str, object] = {"title": title}
    if metric:
        body["metric"] = metric
    if target is not None:
        body["target"] = target
    if deadline:
        body["deadline"] = deadline

    goal = _server_or_exit(lambda: rest_client.post("/me/autonomous/goals", body))
    typer.echo(f"created id={goal.get('id')} title={goal.get('title')}")


@app.command("show")
def show(goal_id: str = typer.Argument(..., metavar="ID")) -> None:
    """Показать цель + milestones + сегодняшний план."""
    goal = _server_or_exit(lambda: rest_client.get(f"/me/autonomous/goals/{goal_id}"))
    typer.echo(f"id:       {goal.get('id')}")
    typer.echo(f"title:    {goal.get('title')}")
    typer.echo(f"metric:   {goal.get('metric', '-')}")
    typer.echo(f"target:   {goal.get('target', '-')}")
    typer.echo(f"status:   {goal.get('status', '-')}")
    milestones = goal.get("milestones") or []
    if milestones:
        typer.echo("milestones:")
        for m in milestones:
            done = "x" if m.get("done") else " "
            typer.echo(f"  [{done}] {m.get('title')}")
    plan = goal.get("today_plan") or []
    if plan:
        typer.echo("today:")
        for step in plan:
            typer.echo(f"  - {step}")


def _set_status(goal_id: str, status: str) -> None:
    result = _server_or_exit(
        lambda: rest_client.post(f"/me/autonomous/goals/{goal_id}/{status}", {})
    )
    typer.echo(f"{status} id={(result or {}).get('id', goal_id)}")


@app.command("pause")
def pause(goal_id: str = typer.Argument(..., metavar="ID")) -> None:
    """Поставить цель на паузу."""
    _set_status(goal_id, "pause")


@app.command("resume")
def resume(goal_id: str = typer.Argument(..., metavar="ID")) -> None:
    """Снять цель с паузы."""
    _set_status(goal_id, "resume")
