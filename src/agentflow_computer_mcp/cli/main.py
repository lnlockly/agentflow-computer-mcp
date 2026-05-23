"""agentflow — CLI for local agents and cloud goals."""
from __future__ import annotations

import typer

from .. import __version__
from . import agent, auth_cli, budget, daemon, goal, memory

app = typer.Typer(
    name="agentflow",
    help="AgentFlow CLI: локальные агенты + облачные цели.",
    no_args_is_help=True,
    add_completion=False,
)

app.add_typer(daemon.app, name="daemon")
app.add_typer(agent.app, name="agent")
app.add_typer(goal.app, name="goal")
app.add_typer(memory.app, name="memory")
app.add_typer(budget.app, name="budget")


@app.command("login")
def login_cmd(
    api_key: str | None = typer.Option(None, "--api-key", help="API ключ AgentFlow"),
) -> None:
    """Сохранить API ключ."""
    auth_cli.do_login(api_key)


@app.command("whoami")
def whoami_cmd() -> None:
    """Показать профиль + устройства."""
    auth_cli.do_whoami()


@app.command("version")
def version_cmd() -> None:
    """Показать версию CLI."""
    typer.echo(__version__)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
