"""`agentflow memory recall/record-skill`."""
from __future__ import annotations

import typer

from . import rest_client
from .format import table

app = typer.Typer(help="Память агентов: уроки и навыки.", no_args_is_help=True)


@app.command("recall")
def recall(query: str = typer.Argument(..., help="Поисковый запрос")) -> None:
    """Найти релевантные уроки в памяти."""
    try:
        data = rest_client.get("/me/memory/search", params={"q": query})
    except rest_client.NotAuthenticated as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=4) from exc
    except rest_client.ServerError as exc:
        typer.echo(f"server: {exc}", err=True)
        raise typer.Exit(code=6) from exc

    if isinstance(data, dict):
        items = data.get("results") or data.get("items") or data.get("memories") or []
    else:
        items = data or []
    items = [x for x in items if isinstance(x, dict)]
    rows = [
        {
            "id": m.get("id", ""),
            "kind": m.get("kind", ""),
            "title": (m.get("title") or m.get("summary") or "")[:60],
            "score": f"{m.get('score', 0.0):.2f}" if isinstance(m.get("score"), (int, float)) else "",
        }
        for m in items
    ]
    typer.echo(table(rows, ["id", "kind", "title", "score"]))


@app.command("record-skill")
def record_skill(
    name: str = typer.Argument(..., help="Имя навыка"),
    steps: str = typer.Option(..., "--steps", help="Шаги (markdown / текст)"),
) -> None:
    """Идемпотентно сохранить навык."""
    try:
        result = rest_client.post(
            "/me/memory/skills",
            {"name": name, "steps": steps},
        )
    except rest_client.NotAuthenticated as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=4) from exc
    except rest_client.ServerError as exc:
        typer.echo(f"server: {exc}", err=True)
        raise typer.Exit(code=6) from exc
    typer.echo(f"saved id={result.get('id')} name={result.get('name', name)}")
