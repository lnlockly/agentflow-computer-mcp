"""`agentflow budget` — daily LLM spend."""
from __future__ import annotations

import typer

from . import rest_client
from .format import fmt_budget

app = typer.Typer(help="Дневной бюджет на LLM.", invoke_without_command=True)


@app.callback()
def budget() -> None:
    """Показать сегодняшний расход и лимит."""
    try:
        data = rest_client.get("/me/autonomous/budget")
    except rest_client.NotAuthenticated as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=4) from exc
    except rest_client.ServerError as exc:
        typer.echo(f"server: {exc}", err=True)
        raise typer.Exit(code=6) from exc

    spent = float((data or {}).get("spent_usd") or 0.0)
    cap = float((data or {}).get("cap_usd") or 0.0)
    typer.echo(fmt_budget(spent, cap))
